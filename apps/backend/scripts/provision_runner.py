"""One-shot workspace-provision runner. The lightbulb-{env}-provision Step
Functions state machine (``core.services.provision_dispatch`` starts it) runs it
as a Lambda (``scripts.provision_lambda``); ``main`` is the manual CLI.

Exit codes: 0 provisioned, 10 deterministic failure (no retry), 11 transient
failure (retried). ``provision_lambda`` maps them to the typed errors the
machine's Retry/Catch match on.

Three legs, in order: ``prepare_hermes_workspace`` settles the owner's tokens
and boot secret; step 0 seeds the owner's workspace row and starter roster (see
``_seed_owner_workspace``); the runtime leg provisions the workspace's
AgentCore runtime and writes every roster profile
(``hermes_provisioning.provision_hermes_runtime``) — a NO-OP when the
workspace is already running. Then ``reconcile_runtime`` makes the (now
running) runtime match billing, so a provider or plan that changed while this
execution ran still lands, and a dispatch against a live workspace is a plain
reconcile. Its failure is reported by its own metric, never as a provision
failure: the runtime it would have adjusted is already serving.

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


async def _seed_owner_workspace(owner_id: str) -> int:
    """Step 0: seed the OWNER workspace (META row + starter roster, no external call) BEFORE
    the runtime work. Returns an exit code from the runner's ABI (0 ok / 10 deterministic /
    11 transient).

    The provisioner is the Clerk org CREATOR (``get_organization().created_by``): always an
    admin, and a stable per-org choice (the dispatch carries only ``owner_id``). A missing
    creator is a Clerk blip, so transient. A seed failure is classified like a runtime failure
    (``classify_provision_error``). Org-only: a non-org owner is refused (deterministic).

    After the seed, ``proactive.ensure`` retires old Ideas/Feed schedules; its failure
    is logged, not classified: the daily schedule reconcile retries the cleanup.
    """
    from core.auth import is_org_owner_id
    from core.repositories.container_repo import classify_provision_error
    from core.services import clerk_admin
    from core.services import proactive
    from core.services.user_provision import _ensure_workspace_for_org

    if not is_org_owner_id(owner_id):
        logger.error("seed step 0: owner %s is not a workspace (org) — refusing to provision", owner_id)
        return EXIT_DETERMINISTIC

    try:
        org = await clerk_admin.get_organization(owner_id)
        creator_user_id = (org or {}).get("created_by")
        if not creator_user_id:
            logger.warning("seed step 0: org %s has no resolvable Clerk creator — deferring (transient)", owner_id)
            return EXIT_TRANSIENT
        await _ensure_workspace_for_org(org_id=owner_id, user_id=creator_user_id, is_admin=True)
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        logger.exception("seed step 0 (owner workspace) failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC
    try:
        await proactive.ensure(owner_id)
    except Exception:  # noqa: BLE001 -- the daily schedule reconcile ensures it again
        logger.exception("seed step 0: proactive ensure failed for owner %s", owner_id)
    return EXIT_OK


async def run(owner_id: str) -> int:
    from core.auth import is_org_owner_id
    from core.observability.logging import bind_log_context

    bind_log_context(user_id=owner_id)

    # M4-R33: mint/write hook_token + gateway_token (+ the boot secret) before the runtime leg.
    # A row that already has a runtime already has its tokens, and the reconcile below
    # prepares it -- running prepare here too would rewrite the boot secret first and hide
    # that boot-time change from the reconcile's restart decision (C1: a warm session kept
    # a bearer nothing restarts it off).
    from core.repositories import container_repo

    try:
        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        if not row.get("agentcore_runtime_arn"):
            await hermes_provisioning.prepare_hermes_workspace(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified + recorded
        return await _record_failure(owner_id, exc, "prepare")

    seed_exit = await _seed_owner_workspace(owner_id)
    if seed_exit == EXIT_DETERMINISTIC:
        await _mark_error(
            owner_id,
            "provision refused: owner is not a workspace (org)"
            if not is_org_owner_id(owner_id)
            else "provision seed: owner workspace seed failed (see log)",
            retryable=False,
        )
    if seed_exit != EXIT_OK:
        # A transient failure writes nothing: the machine retries it, and its MarkFailedExhausted
        # writes the error row if the retries run out (see _record_failure).
        return seed_exit
    await _drain_pending_brief(owner_id)
    await _seat_workspace_rooms(owner_id)

    try:
        await hermes_provisioning.provision_hermes_runtime(owner_id)
    except Exception as exc:  # noqa: BLE001 — classified + recorded
        return await _record_failure(owner_id, exc, "runtime provision")
    await _arm_trial(owner_id)
    await hermes_provisioning.reconcile_runtime(owner_id)
    return EXIT_OK


#: Bound on the brief drain inside the provisioner Lambda: the drain retries with sleeps of its
#: own (onboarding_brief._RETRY_BACKOFF_S, ~3 min), which must not eat the runtime leg's budget.
BRIEF_DRAIN_TIMEOUT_S = 30.0


async def _drain_pending_brief(owner_id: str) -> None:
    """Step 0 just made the workspace active: fulfil a first-room message captured
    before it was (``onboarding_brief.capture_first_workspace_message`` drains its own capture
    too). Idempotent and claim-fenced; a no-op read when nothing is pending. Never fails the
    provision -- an unfinished drain leaves the brief PENDING for the next drain."""
    from core.observability.metrics import put_metric
    from core.services.onboarding_brief import reconcile_pending_brief

    try:
        await asyncio.wait_for(reconcile_pending_brief(owner_id), timeout=BRIEF_DRAIN_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — incl. TimeoutError; reconcile_pending_brief never raises itself
        logger.exception("provision: pending onboarding-brief drain failed for owner %s", owner_id)
        put_metric("onboarding_brief.provision_drain.error")


async def _seat_workspace_rooms(owner_id: str) -> None:
    """Step 0 just made the workspace active: create the vendor shared channel and seat every
    current teammate in ``#general``. A member who joined before the room existed was missed by
    the ``organizationMembership.created`` webhook's seat. Both are idempotent (a removed member
    is never re-seated: the AUTOJOIN marker stays). Never fails the provision."""
    from core.observability.metrics import put_metric
    from core.services.clerk_admin import list_organization_members
    from core.services.messaging import ensure_vendor_room, join_default_room, join_feed_room

    try:
        await ensure_vendor_room(owner_id)
    except Exception:  # noqa: BLE001
        logger.exception("provision: vendor room reconcile failed for owner %s", owner_id)
        put_metric("messaging.vendor_room.error")
    try:
        for member in await list_organization_members(owner_id):
            try:
                await join_default_room(owner_id, member["user_id"])
            except Exception:  # noqa: BLE001
                logger.exception(
                    "provision: default room join failed for owner %s user %s", owner_id, member["user_id"]
                )
                put_metric("messaging.default_room.error")
            try:
                await join_feed_room(owner_id, member["user_id"])
            except Exception:  # noqa: BLE001
                logger.exception("provision: feed join failed for owner %s user %s", owner_id, member["user_id"])
                put_metric("messaging.feed_room.error")
    except Exception:  # noqa: BLE001 — a roster read failure never fails the provision
        logger.exception("provision: teammate roster read failed for owner %s", owner_id)
        put_metric("messaging.default_room.error")


async def _arm_trial(owner_id: str) -> None:
    """The runtime is created and the row ``running``: start an unarmed ``trial_local`` clock,
    once (a conditional write -- a no-op for a paid or already-armed row). Never fails the
    provision: an unarmed trial simply has not started counting yet."""
    from core.observability.metrics import put_metric
    from core.repositories import billing_repo

    try:
        await billing_repo.arm_trial_if_unset(owner_id)
    except Exception:  # noqa: BLE001
        logger.exception("provision: trial arm failed for owner %s", owner_id)
        put_metric("provision.trial_arm.error")


async def _record_failure(owner_id: str, exc: Exception, stage: str) -> int:
    """Classify a Hermes-leg failure, record it on the row, map it to the exit-code ABI —
    so the stepper's Retry (and the SFN retry loop) can act on it instead of an open spinner."""
    from core.repositories.container_repo import classify_provision_error
    from core.services.openrouter_keys import KeyClaimConflict

    logger.exception("hermes %s failed for owner %s", stage, owner_id)
    if isinstance(exc, KeyClaimConflict):
        # The claim loser: a concurrent attempt for this SAME owner already won the
        # OpenRouter key claim and is finishing it (normally a couple of seconds).
        # Retry without writing status="error" -- that write is what the user saw
        # flash as a provision error for a condition that self-heals on the SFN's
        # own 10s retry. A genuinely stuck claim (crashed winner) still surfaces:
        # the claim row's 300s TTL bounds it, and the SFN's own retry-exhausted
        # backstop (service-stack.ts markFailedExhausted) writes the error row if
        # retries run out first.
        return EXIT_TRANSIENT
    _, retryable = classify_provision_error(exc)
    if retryable:
        # Same reasoning as the claim loser above: the SFN retries a transient failure within
        # this execution, so an error row now would flash "Setup failed" over a provision that
        # is about to succeed. MarkFailedExhausted writes it if the retries run out -- but it
        # carries no reason, so record the reason now (status untouched).
        await _note_error(owner_id, f"hermes {stage} failed: {type(exc).__name__}: {exc}")
        return EXIT_TRANSIENT
    await _mark_error(owner_id, f"hermes {stage} failed: {exc}", retryable=False)
    return EXIT_DETERMINISTIC


async def _note_error(owner_id: str, last_error: str) -> None:
    from core.repositories import container_repo

    try:
        await container_repo.note_provision_error(owner_id, last_error)
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("provision: error note failed for owner %s", owner_id)


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
