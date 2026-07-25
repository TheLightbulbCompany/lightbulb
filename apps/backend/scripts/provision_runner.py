"""One-shot container-provision runner, executed as an ECS task by the
isol8-{env}-provision Step Functions state machine (never by the backend).

Exit codes are the machine's ABI (spec §7 / design doc §3): 0 provisioned+
healthy, 10 deterministic failure (no retry), 11 transient failure (machine
retries), 20 superseded by a deliberate reprovision (machine stops quietly).

The runner owns the provisioning -> running transition in the FOREGROUND
(wait_for_running) — the detached create_task pollers ecs_manager fires
internally for non-provisioning paths (wake, resize) never run inside this
process; the foreground wait is the one that matters here, and every row
write is condition-guarded (claim + generation + per-attempt token).

PROVISION_ATTEMPT_TOKEN (env, set by the state machine's containerOverrides
from ``$.attempt_token``) is this ATTEMPT's fencing token — fresh every SFN
retry (``BumpAttempt`` mints one via ``States.UUID()``), while ``--claim``
and the execution name stay stable across the whole execution. The runner's
first action is always to (re-)stamp it via ``container_repo.claim_attempt``,
which is what fences a still-running straggler from a superseded attempt out
at its next checkpoint — see that function's docstring for the full hazard
this closes (spec §7 hazard 1). Falls back to ``--claim`` itself when unset
(a bare/local invocation outside the state machine — attempt-0-equivalent).

``provision_generation`` (the container-incarnation fence, INV-2) is read
directly off the containers row at startup rather than threaded through the
state machine's input/command line: StartExecution's Branch-A idempotency
(core.services.provision_dispatch) requires the dispatch INPUT to stay
byte-identical across re-issues for the SAME claim, but generation can be
MINTED mid-execution (by create_user_service, inside THIS run) — embedding
it in the deterministic input would make a later Branch-A re-issue (a
concurrent status-poll re-affirming the same in-flight execution) recompute
a different input, misclassify the still-RUNNING execution as "closed", and
dispatch a duplicate. Reading it fresh off the row side-steps that.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Module-level (unlike the runner's otherwise-lazy core.* imports): the
# SEED_IN_EXECUTOR flag is read on every run() and core.config is the lightest,
# most universally-imported module in the app — no import-graph cost, and it
# keeps the flag check patchable via provision_runner.settings in tests.
from core.config import settings

EXIT_OK = 0
EXIT_DETERMINISTIC = 10
EXIT_TRANSIENT = 11
EXIT_SUPERSEDED = 20

logger = logging.getLogger("provision_runner")


def _get_manager():
    from core.containers import get_ecs_manager

    return get_ecs_manager()


async def _resolve_provider(owner_id: str):
    from core.services.container_provision import resolve_provider_choice

    return await resolve_provider_choice(owner_id)


async def _get_row(owner_id: str):
    from core.repositories import container_repo

    return await container_repo.get_by_owner_id(owner_id)


async def _seed_owner_paperclip(owner_id: str) -> int:
    """Step 0 (SEED_IN_EXECUTOR only): run the OWNER Paperclip seed under the
    executor's single-flight, BEFORE the container work. Returns an exit code
    from the runner's ABI (0 ok / 10 deterministic / 11 transient).

    WHY here: GET /container/status used to run provision_org (Better-Auth
    signup + company + Friday/Atlas) on the poll thread, where load-balanced
    poll fan-out double-ran it and double-created the company (#1442). The
    executor elects exactly one runner per owner (try_claim_provision), so
    seeding here inherits that single-flight — one runner, one provision_org,
    structurally no duplicate company. Gated behind settings.SEED_IN_EXECUTOR;
    when OFF this is never called and the poll thread seeds exactly as before.

    Reuses the SAME service helpers ensure_user_ready uses
    (``_ensure_paperclip_for_owner`` / ``_ensure_paperclip_for_org``) — no seed
    logic is duplicated. Those helpers keep their own swallows: 409 (a benign
    concurrent-seed no-op) and 422 (a duplicate-email orphan, recorded as a
    paperclip-row failure — Teams degraded, but the container must still boot,
    so it is NOT a reason to fail the whole provision). Only a GENUINE seed
    failure — a non-409/422 Paperclip error, a timeout/connection error, or a
    config bug — propagates here, where it is classified into the exit-code ABI
    exactly like a container failure (``classify_provision_error``): transient
    -> EXIT_TRANSIENT (SFN retries), deterministic -> EXIT_DETERMINISTIC (SFN
    stops). This ties seed success to the execution instead of the old
    poll-thread swallow that booted the box regardless.

    Org-vs-personal is derived from the ``owner_id`` prefix
    (``is_org_owner_id``). The dispatch carries ONLY owner_id
    (``background_provision`` drops the triggering user_id, and threading it
    into the execution input would break Branch-A idempotency — the input must
    stay byte-identical across a claim's re-issues, but the triggering admin
    can differ between two polls). So the org path resolves the company's
    bootstrap identity from Clerk: the org CREATOR
    (``get_organization().created_by``) — always an admin, and a STABLE
    per-org choice the executor's determinism prefers over "whichever admin's
    poll won the race". ``owner_email`` is resolved via ``lookup_owner_email``
    (the runner has Clerk creds), the same fallback the helpers use for a JWT
    without an email claim; passing it in avoids a redundant second lookup
    inside the helper.
    """
    from core.auth import is_org_owner_id
    from core.repositories.container_repo import classify_provision_error
    from core.services import clerk_admin
    from core.services.paperclip_owner_email import lookup_owner_email
    from core.services.user_provision import (
        _ensure_paperclip_for_org,
        _ensure_paperclip_for_owner,
    )

    try:
        if is_org_owner_id(owner_id):
            org = await clerk_admin.get_organization(owner_id)
            creator_user_id = (org or {}).get("created_by")
            if not creator_user_id:
                # No resolvable bootstrap identity for the org. A Clerk hiccup
                # (or an unset CLERK_SECRET_KEY in a misconfigured env) is the
                # likely cause — a genuinely creator-less Clerk org is
                # vanishingly rare — so treat it as transient and let SFN retry
                # rather than fail the provision on a momentary blip.
                logger.warning(
                    "seed step 0: org %s has no resolvable Clerk creator — deferring (transient)",
                    owner_id,
                )
                return EXIT_TRANSIENT
            owner_email = await lookup_owner_email(org_id=owner_id, fallback_user_id=creator_user_id) or ""
            # is_admin=True: the org creator is definitionally an org admin, so
            # the create branch of _ensure_paperclip_for_org is authorized. When
            # the company already exists (an idempotent re-run) this same call
            # falls through to the JOIN path and no-ops on the creator's already
            # -active owner row.
            await _ensure_paperclip_for_org(
                org_id=owner_id,
                user_id=creator_user_id,
                email=owner_email,
                is_admin=True,
            )
        else:
            owner_email = await lookup_owner_email(org_id=None, fallback_user_id=owner_id) or ""
            await _ensure_paperclip_for_owner(
                owner_id=owner_id,
                owner_user_id=owner_id,
                owner_email=owner_email,
                org_id=None,
            )
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        logger.exception("seed step 0 (owner paperclip) failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC
    return EXIT_OK


async def run(owner_id: str, claim_id: str) -> int:
    from core.auth import is_org_owner_id
    from core.observability.logging import bind_log_context
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded, classify_provision_error

    bind_log_context(user_id=owner_id)
    manager = _get_manager()
    # Attempt-token fencing (hazard 1, spec §7): every attempt -- including
    # attempt 0, where this is a same-value no-op re-assertion -- re-stamps
    # the row's provision_attempt_token BEFORE any other checkpoint. See
    # container_repo.claim_attempt.
    attempt_token = os.environ.get("PROVISION_ATTEMPT_TOKEN") or claim_id
    try:
        await container_repo.claim_attempt(owner_id, claim_id, attempt_token)

        # Step 0 (SEED_IN_EXECUTOR): the OWNER Paperclip seed runs HERE, under
        # the executor's single-flight (this is the ONLY runner for this
        # owner's claim), BEFORE the container work — killing the
        # duplicate-company race (#1442) at its source. Inert unless the flag is
        # on (default OFF -> the poll thread seeds, byte-identical to today).
        #
        # Placed AFTER claim_attempt so a straggler whose claim was taken over
        # stands down (ProvisionSuperseded) before it seeds. On a genuine seed
        # failure we make the container row HONEST: flip it to error with the
        # classified retryability (transient -> re-claimable/SFN-retried,
        # deterministic -> surfaced) BEFORE returning the mapped exit code — the
        # same release_provision_to_error pattern provision_user_container uses
        # for its own failures — so seed success is tied to the execution rather
        # than silently swallowed.
        if settings.SEED_IN_EXECUTOR:
            seed_exit = await _seed_owner_paperclip(owner_id)
            if seed_exit != EXIT_OK:
                try:
                    await container_repo.release_provision_to_error(
                        owner_id,
                        claim_id,
                        last_error="provision seed: owner paperclip company seed failed",
                        retryable=(seed_exit == EXIT_TRANSIENT),
                        attempt_token=attempt_token,
                    )
                except Exception:  # noqa: BLE001 — release is best-effort; the exit code is the ABI
                    logger.exception("seed step 0: error release failed for owner %s", owner_id)
                return seed_exit

        # Read the container-incarnation fence off the row (see module
        # docstring for why this is NOT threaded through the SFN input).
        # Absent for a brand-new owner -- create_user_service mints it later,
        # inside provision_user_container below.
        pre_row = await container_repo.get_by_owner_id(owner_id)
        generation = (pre_row or {}).get("provision_generation")

        provider_choice, byo_provider = await _resolve_provider(owner_id)
        owner_type = "org" if is_org_owner_id(owner_id) else "personal"
        await manager.provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            owner_type=owner_type,
            claim_id=claim_id,
            generation=generation,
            attempt_token=attempt_token,
        )
        # The full-provision path (create_user_service) MINTS a fresh
        # provision_generation — for a brand-new owner AND for an existing
        # row whose ECS service was missing/INACTIVE. Re-read so
        # wait_for_running fences on the CURRENT incarnation, not the
        # pre-create value read above (which would make its own running
        # write raise ProvisionSuperseded against this very run and wedge
        # the row in "provisioning"). claim_id/attempt_token stay the
        # primary fences.
        post_row = await container_repo.get_by_owner_id(owner_id)
        generation = (post_row or {}).get("provision_generation")
        await manager.wait_for_running(
            owner_id,
            claim_id=claim_id,
            generation=generation,
            attempt_token=attempt_token,
        )
    except ProvisionSuperseded:
        return await _stand_down_superseded(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        # provision_user_container already released the lease to error with
        # cause fields (via the same shared classifier); the exit code only
        # tells the machine whether to retry.
        logger.exception("provision failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC

    row = await _get_row(owner_id)
    status = (row or {}).get("status")
    if status == "running":
        return EXIT_OK
    if status == "error":
        return EXIT_TRANSIENT if (row or {}).get("retryable", True) else EXIT_DETERMINISTIC
    # Any other status (stopped/deleted/provisioning-under-new-lease): an
    # external actor owns the row now — succeed quietly, write nothing.
    logger.info("owner %s left provisioning externally (status=%s)", owner_id, status)
    return EXIT_OK


async def _stand_down_superseded(owner_id: str) -> int:
    """The superseded exit — but VERIFIED, not assumed (#1428).

    ``ProvisionSuperseded`` used to be treated as proof that a successor owns
    the row, so the runner exited 20 and the machine stood down quietly. That
    assumption failed live: a conversion-marker get→put clobbered the row —
    resurrecting status="stopped" and ERASING the lease — so this runner's
    next checkpoint saw a token mismatch and "stood down" behind a successor
    that did not exist. Terminal state: stopped, no claim, no error, nothing
    running and nothing scheduled; only a user-visit status poll healed it.

    So before exiting, re-read the row and check who actually owns it:

    - row gone, or a claim present → a real successor (or a deliberate
      delete). The quiet exit was right; keep it. A present claim's own
      liveness is dispatch Branch A / stale-lease territory — the runner
      must not second-guess it from out here.
    - NO claim + status "stopped" → the strand. Flip it to error(retryable)
      via the ``release_stranded_stop_to_error`` CAS (atomic on exactly that
      shape, so a claim landing between the read and the write wins and the
      flip no-ops) and log LOUDLY — the poll's error auto-retry now recovers
      the row on the owner's next poll instead of never.
    - NO claim + "provisioning" → an unfenced wake/resize transition owns it
      (those paths never claim); its in-process poller — or the stale-lease
      heuristic after it — is the recovery path. Log, do not touch.
    - NO claim + "error" → already in the self-healing state the flip would
      produce. Log only.

    Always returns EXIT_SUPERSEDED — the machine's ABI is unchanged; the row
    write (not the exit code) is what re-arms recovery.
    """
    from core.repositories import container_repo

    try:
        row = await container_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — verification is best-effort
        logger.exception(
            "provision superseded for owner %s — row verification failed, standing down blind",
            owner_id,
        )
        return EXIT_SUPERSEDED
    if row is None or row.get("provision_claim_id"):
        logger.info(
            "provision superseded for owner %s — standing down (successor claim present=%s, status=%s)",
            owner_id,
            bool(row and row.get("provision_claim_id")),
            (row or {}).get("status"),
        )
        return EXIT_SUPERSEDED

    status = row.get("status")
    if status == "stopped":
        released = False
        try:
            released = await container_repo.release_stranded_stop_to_error(
                owner_id,
                last_error="provision superseded with no successor claim — released for auto-retry",
            )
        except Exception:  # noqa: BLE001 — the flip is best-effort; the exit code stays the ABI
            logger.exception("provision superseded for owner %s — stranded-row release failed", owner_id)
        if released:
            logger.error(
                "provision superseded for owner %s with NO successor: row was stopped with no claim "
                "and no error — flipped to error(retryable) so the status poll re-dispatches",
                owner_id,
            )
        else:
            logger.warning(
                "provision superseded for owner %s: stopped row with no claim, but the stranded-row "
                "release did not land (row moved on, or parked trial) — standing down",
                owner_id,
            )
    elif status == "error":
        logger.warning(
            "provision superseded for owner %s with no successor claim — row already error "
            "(retryable=%s); the status poll's auto-retry owns recovery",
            owner_id,
            row.get("retryable", True),
        )
    else:
        logger.warning(
            "provision superseded for owner %s with no successor claim (status=%s) — an unfenced "
            "wake/resize transition or external actor owns the row; standing down",
            owner_id,
            status,
        )
    return EXIT_SUPERSEDED


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--claim", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    sys.exit(asyncio.run(run(args.owner, args.claim)))


if __name__ == "__main__":
    main()
