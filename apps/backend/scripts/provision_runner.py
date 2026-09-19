"""One-shot container-provision runner.

Two invocation modes, same code path and the same exit-code ABI:
- INLINE (the fast path): the backend's dispatcher runs this module as a
  CHILD PROCESS of the backend task as attempt 0
  (``provision_dispatch._run_inline``), skipping the ~30s ECS task
  cold-launch entirely.
- SFN (the durable path): executed as an ECS task by the lightbulb-{env}-provision
  Step Functions state machine — for retries after a failed inline attempt,
  and for claims the backend could not finish (crash / deploy handoff).

Exit codes are the machine's ABI (spec §7 / design doc §3): 0 provisioned+
healthy, 10 deterministic failure (no retry), 11 transient failure (machine
retries), 20 superseded by a deliberate reprovision (machine stops quietly).

Two legs, in order: step 0 seeds the owner's Paperclip company (see
``_seed_owner_paperclip``), then the container leg provisions ECS — and the
container leg is a NO-OP when the owner's box is already up and healthy (see
``_container_already_healthy``). That is what makes the single dispatch safe
for callers whose container is already running: they get the seed reconcile
without a force-redeploy.

The runner owns the provisioning -> running transition in the FOREGROUND, and every row write
is condition-guarded (claim + generation + per-attempt token).

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


# Imported eagerly (unlike this module's other core.* imports, which are lazy inside
# functions) so tests can monkeypatch `provision_runner.hermes_provisioning` directly.
from core.services import hermes_provisioning

EXIT_OK = 0
EXIT_DETERMINISTIC = 10
EXIT_TRANSIENT = 11
EXIT_SUPERSEDED = 20

logger = logging.getLogger("provision_runner")


async def _seed_owner_paperclip(owner_id: str) -> int:
    """Step 0: run the OWNER Paperclip seed under the executor's single-flight,
    BEFORE the container work. Returns an exit code from the runner's ABI
    (0 ok / 10 deterministic / 11 transient).

    WHY here: GET /container/status used to run provision_org (Better-Auth
    signup + company + starter roster) on the poll thread, where load-balanced
    poll fan-out double-ran it and double-created the company. The
    executor elects exactly one runner per owner (try_claim_provision), so
    seeding here inherits that single-flight — one runner, one provision_org,
    structurally no duplicate company. This is now THE owner-seed path: the
    poll thread keeps only the org member JOIN (see
    ``core.services.user_provision.ensure_user_ready``).

    Reuses the SAME service helper ensure_user_ready uses
    (``_ensure_paperclip_for_org``) — no seed logic is duplicated. It keeps
    its own swallows: 409 (a benign concurrent-seed no-op) and 422 (a
    duplicate-email orphan, recorded as a paperclip-row failure — Teams
    degraded, but the container must still boot,
    so it is NOT a reason to fail the whole provision). Only a GENUINE seed
    failure — a non-409/422 Paperclip error, a timeout/connection error, or a
    config bug — propagates here, where it is classified into the exit-code ABI
    exactly like a container failure (``classify_provision_error``): transient
    -> EXIT_TRANSIENT (SFN retries), deterministic -> EXIT_DETERMINISTIC (SFN
    stops). This ties seed success to the execution instead of the old
    poll-thread swallow that booted the box regardless.

    Org-only. Personal workspaces no longer exist (the September 2026 cohort
    was purged or migrated), so a non-org ``owner_id`` is refused with
    EXIT_DETERMINISTIC before anything is seeded — the executor's own fence,
    matching ``ensure_user_ready``'s refusal of a no-org caller. The dispatch
    carries ONLY owner_id (the dispatch path drops the triggering user_id, and
    threading it into the execution input would break Branch-A idempotency —
    the input must stay byte-identical across a claim's re-issues, but the
    triggering admin can differ between two polls). So step 0 resolves the
    company's bootstrap identity from Clerk: the org CREATOR
    (``get_organization().created_by``) — always an admin, and a STABLE
    per-org choice the executor's determinism prefers over "whichever admin's
    poll won the race". ``owner_email`` is resolved via ``lookup_owner_email``
    (the runner has Clerk creds), the same fallback the helpers use for a JWT
    without an email claim; passing it in avoids a redundant second lookup
    inside the helper.

    After the seed, ``arm_nightly_ceo`` arms the nightly-CEO routine on the
    freshly seeded CEO. It is gated INSIDE on ``should_arm_nightly_ceo`` — paid
    OR an unexpired ``trial_local``; an expired trial is a no-op — and arming
    here, not only on a UI poll, is what makes it deterministic for org
    first-provisions and trial→paid conversions, both of which reach this
    executor. Its failures classify exactly like the seed's.
    ``trust_cache=False``: a retry of a run that died between the arm's claim
    write and its routine create must not trust the claim cache.
    """
    from core.auth import is_org_owner_id
    from core.repositories.container_repo import classify_provision_error
    from core.services import clerk_admin
    from core.services.paperclip_owner_email import lookup_owner_email
    from core.services.user_provision import _ensure_paperclip_for_org, arm_nightly_ceo

    if not is_org_owner_id(owner_id):
        logger.error("seed step 0: owner %s is not a workspace (org) — refusing to provision", owner_id)
        return EXIT_DETERMINISTIC

    try:
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
        await _ensure_paperclip_for_org(org_id=owner_id, user_id=creator_user_id, email=owner_email, is_admin=True)
        await arm_nightly_ceo(owner_id, trust_cache=False)
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        logger.exception("seed step 0 (owner paperclip) failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC
    return EXIT_OK


async def run(owner_id: str, claim_id: str) -> int:
    from core.auth import is_org_owner_id
    from core.observability.logging import bind_log_context
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    bind_log_context(user_id=owner_id)

    # Attempt-token fencing (hazard 1, spec §7): every attempt -- including
    # attempt 0, where this is a same-value no-op re-assertion -- re-stamps
    # the row's provision_attempt_token BEFORE any other checkpoint. See
    # container_repo.claim_attempt. The inline dispatcher sets the env var to
    # its own distinct token (provision_dispatch.inline_attempt_token) so a
    # later SFN takeover of the same claim fences an inline straggler out at
    # its next checkpoint.
    attempt_token = os.environ.get("PROVISION_ATTEMPT_TOKEN") or claim_id
    try:
        await container_repo.claim_attempt(owner_id, claim_id, attempt_token)

        # M4-R33: mint/write hook_token + gateway_token (+ the boot secret) BEFORE step 0
        # seeds the roster. Step 0's starter-agent loop bakes hook_token into the http
        # adapter's Authorization header at seed time — seeding first shipped every starter
        # agent with an EMPTY bearer, since nothing had minted one yet. Fenced, NON-terminal
        # (the provision isn't done): a lost fence here raises ProvisionSuperseded, caught by
        # the same handler as every other checkpoint in this function.
        # C2/M4-R36: any OTHER exception is released to error here — prepare_hermes_workspace
        # has no internal release of its own.
        try:
            await hermes_provisioning.prepare_hermes_workspace(owner_id, claim_id=claim_id, attempt_token=attempt_token)
        except ProvisionSuperseded:
            raise
        except Exception as exc:  # noqa: BLE001 — classified + released; see the helper's docstring
            return await _release_hermes_failure_to_error(owner_id, claim_id, attempt_token, exc, "prepare")

        # Step 0: the OWNER Paperclip seed runs HERE, under the executor's
        # single-flight (this is the ONLY runner for this owner's claim),
        # BEFORE the container work — killing the duplicate-company race
        # at its source.
        #
        # Placed AFTER claim_attempt so a straggler whose claim was taken over
        # stands down (ProvisionSuperseded) before it seeds. On a genuine seed
        # failure we make the container row HONEST: flip it to error with the
        # classified retryability (transient -> re-claimable/SFN-retried,
        # deterministic -> surfaced) BEFORE returning the mapped exit code — the
        # same release_provision_to_error pattern provision_user_container uses
        # for its own failures — so seed success is tied to the execution rather
        # than silently swallowed.
        seed_exit = await _seed_owner_paperclip(owner_id)
        if seed_exit != EXIT_OK:
            try:
                await container_repo.release_provision_to_error(
                    owner_id,
                    claim_id,
                    last_error=(
                        "provision refused: owner is not a workspace (org)"
                        if not is_org_owner_id(owner_id)
                        else "provision seed: owner paperclip seed or nightly-CEO arm failed (see log)"
                    ),
                    retryable=(seed_exit == EXIT_TRANSIENT),
                    attempt_token=attempt_token,
                )
            except Exception:  # noqa: BLE001 — release is best-effort; the exit code is the ABI
                logger.exception("seed step 0: error release failed for owner %s", owner_id)
            return seed_exit

        # CreateAgentRuntime is one synchronous call, so there is no service to poll and no
        # generation to re-read: provision_hermes_runtime writes the terminal row itself,
        # FENCED on the same claim_id/attempt_token this run already holds (M4-R28). A lost
        # fence raises ProvisionSuperseded, caught below.
        # C2/M4-R36: any OTHER exception is released to error here, same as prepare's own
        # branch above — provision_hermes_runtime has no internal release of its own.
        try:
            await hermes_provisioning.provision_hermes_runtime(owner_id, claim_id=claim_id, attempt_token=attempt_token)
        except ProvisionSuperseded:
            raise
        except Exception as exc:  # noqa: BLE001 — classified + released; see the helper's docstring
            return await _release_hermes_failure_to_error(owner_id, claim_id, attempt_token, exc, "runtime provision")
        return EXIT_OK
    except ProvisionSuperseded:
        return await _stand_down_superseded(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        # Every step above already released the lease to error with cause fields; the exit
        # code only tells the machine whether to retry.
        from core.repositories.container_repo import classify_provision_error

        logger.exception("provision failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC


async def _release_hermes_failure_to_error(
    owner_id: str, claim_id: str, attempt_token: str | None, exc: Exception, stage: str
) -> int:
    """C2/M4-R36: the Hermes leg's own error-release call — same shape as step 0's (classify,
    release_provision_to_error, map to the exit-code ABI), and the same job
    ``provision_user_container`` does INTERNALLY for the ECS leg (see the comment on the bare
    ``except Exception`` below). Without this, a raise out of ``prepare_hermes_workspace`` or
    ``provision_hermes_runtime`` left ``status="provisioning"`` with a LIVE claim and no
    ``last_error``/``retryable`` — an open-ended spinner for the owner and nothing for the SFN
    watch loop's error branch or the frontend's error auto-retry to act on.
    """
    from core.repositories import container_repo
    from core.repositories.container_repo import classify_provision_error

    logger.exception("hermes %s failed for owner %s", stage, owner_id)
    _, retryable = classify_provision_error(exc)
    try:
        await container_repo.release_provision_to_error(
            owner_id,
            claim_id,
            last_error=f"hermes {stage} failed: {exc}",
            retryable=retryable,
            attempt_token=attempt_token,
        )
    except Exception:  # noqa: BLE001 — release is best-effort; the exit code is the ABI
        logger.exception("hermes %s: error release failed for owner %s", stage, owner_id)
    return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC


async def _stand_down_superseded(owner_id: str) -> int:
    """The superseded exit — but VERIFIED, not assumed.

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
