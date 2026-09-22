"""One-shot workspace-provision runner, executed as an ECS task by the
lightbulb-{env}-provision Step Functions state machine
(``core.services.provision_dispatch`` starts it).

Exit codes are the machine's ABI: 0 provisioned, 10 deterministic failure (no
retry), 11 transient failure (machine retries).

Three legs, in order: ``prepare_hermes_workspace`` settles the owner's tokens
and boot secret; step 0 seeds the owner's Paperclip company (see
``_seed_owner_paperclip``); the runtime leg provisions the workspace's
AgentCore runtime and writes every roster profile
(``hermes_provisioning.provision_hermes_runtime``) — a NO-OP when the
workspace is already running. That is what makes a dispatch safe for callers
whose workspace is already up: they get the seed reconcile and nothing else.

No lease: every step is idempotent at its source, so two concurrent runs for
one owner converge. A failure is recorded on the row with its retryability
(``container_repo.mark_provision_error``) before the mapped exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


# Imported eagerly (unlike this module's other core.* imports, which are lazy inside
# functions) so tests can monkeypatch `provision_runner.hermes_provisioning` directly.
from core.services import hermes_provisioning

EXIT_OK = 0
EXIT_DETERMINISTIC = 10
EXIT_TRANSIENT = 11

logger = logging.getLogger("provision_runner")


async def _seed_owner_paperclip(owner_id: str) -> int:
    """Step 0: run the OWNER Paperclip seed BEFORE the container work. Returns
    an exit code from the runner's ABI (0 ok / 10 deterministic / 11 transient).

    WHY here: GET /container/status used to run provision_org (Better-Auth
    signup + company + starter roster) on the poll thread, where load-balanced
    poll fan-out double-ran it and double-created the company. This is now THE
    owner-seed path; two concurrent executions are fenced by the seed's own
    durable claims (``try_acquire_org_create_claim`` / the seed claim in
    ``paperclip_provisioning``), which stay until Paperclip company create is
    idempotent in prod.

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
    carries ONLY owner_id (the triggering admin can differ between two polls,
    so it is not a stable input). So step 0 resolves the
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


async def run(owner_id: str) -> int:
    from core.auth import is_org_owner_id
    from core.observability.logging import bind_log_context

    bind_log_context(user_id=owner_id)

    # M4-R33: mint/write hook_token + gateway_token (+ the boot secret) BEFORE step 0 seeds
    # the roster: step 0's starter-agent loop bakes hook_token into the adapter at seed time.
    try:
        await hermes_provisioning.prepare_hermes_workspace(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified + recorded
        return await _record_failure(owner_id, exc, "prepare")

    seed_exit = await _seed_owner_paperclip(owner_id)
    if seed_exit != EXIT_OK:
        await _mark_error(
            owner_id,
            "provision refused: owner is not a workspace (org)"
            if not is_org_owner_id(owner_id)
            else "provision seed: owner paperclip seed or nightly-CEO arm failed (see log)",
            retryable=seed_exit == EXIT_TRANSIENT,
        )
        return seed_exit

    try:
        await hermes_provisioning.provision_hermes_runtime(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified + recorded
        return await _record_failure(owner_id, exc, "runtime provision")
    return EXIT_OK


async def _record_failure(owner_id: str, exc: Exception, stage: str) -> int:
    """Classify a Hermes-leg failure, record it on the row, map it to the exit-code ABI —
    so the status poll (and the SFN retry loop) can act on it instead of an open spinner."""
    from core.repositories.container_repo import classify_provision_error

    logger.exception("hermes %s failed for owner %s", stage, owner_id)
    _, retryable = classify_provision_error(exc)
    await _mark_error(owner_id, f"hermes {stage} failed: {exc}", retryable=retryable)
    return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC


async def _mark_error(owner_id: str, last_error: str, *, retryable: bool) -> None:
    from core.repositories import container_repo

    try:
        await container_repo.mark_provision_error(owner_id, last_error=last_error, retryable=retryable)
    except Exception:  # noqa: BLE001 — best-effort; the exit code is the ABI
        logger.exception("provision: error write failed for owner %s", owner_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    sys.exit(asyncio.run(run(args.owner)))


if __name__ == "__main__":
    main()
