"""Wake-on-intent policy for parked trial containers — the single predicate.

For an ECS row, PARKED iff ``status="stopped"`` AND ``stop_reason ==
"trial_idle"`` (stamped only by the trial teardown ladder) AND the owner is
still ``trial_local`` AND no trial→paid conversion is pending. Parked is the
ONLY state whose wake the status poll may suppress; every other stopped/error
shape keeps its poll-driven self-heal exactly as before (crashed boxes,
killswitch stops, stale provision leases, converting rows).

For an AgentCore row (``runtime_transport.host_for`` == ``"agentcore"``),
there is no stopped/stop_reason to read: AWS parks an idle microVM invisibly
and ``ecs_manager.stop_user_service`` no-ops for this host, so the row stays
``status="running"`` forever (report 04 risk 2). The only honest signal left
is elapsed time since ``last_interactive_at`` against the runtime's own idle
timeout (``agentcore_runtime.IDLE_TIMEOUT_S``) — same trial-only gating,
different park signal.

Both doors that can wake a container off a passive read — the ``/container/
status`` auto-retry and the Teams ``resolve_teams_context`` reconcile — MUST
consult this one module, so they can never disagree about what parked means
(the asymmetry between those two doors was a verified break in the design
review). Explicit intent (``POST /container/wake``, chat send, retry buttons)
never consults the suppress predicate and is never budgeted.

Every public entry point returns ALLOW/no-op when ``TRIAL_WAKE_GATE_ENABLED``
is off, so the CDK flag (or a plain code revert — the suppress marker is a
query param, no CORS surface) restores wake-on-read wholesale.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Dedup for the suppressed metric: emitting on every 3s poll of a parked row
# would be pure spam, so emit once per (owner, park episode, door, reason).
# In-process cache — a restart re-emitting once is fine.
_suppressed_emitted: dict[str, str] = {}


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _is_trial(owner_id: str, account: Mapping[str, Any] | None) -> bool:
    """Read (or reuse) the billing row and say whether this owner is on the free trial.

    Fails OPEN (False) on a missing/foreign account or a billing read failure — shared by
    both the ECS and AgentCore legs of ``is_parked_trial``.
    """
    from core.repositories import billing_repo

    try:
        acct = account if account is not None else await billing_repo.get_by_owner_id(owner_id)
    except Exception:
        logger.exception("wake_policy billing read failed owner=%s — treating as not parked", owner_id)
        return False
    return bool(acct) and acct.get("subscription_status") == "trial_local"


def _last_interactive_epoch(container: Mapping[str, Any]) -> float | None:
    """Normalize ``last_interactive_at`` to a UTC epoch-seconds float, or None if absent/unparseable.

    Production rows store an epoch int stamped by ``container_repo.touch_last_interactive``
    (DynamoDB hands numeric attributes back as ``Decimal`` — ``plain()`` is the repo's existing
    converter). Also accepts an ISO string, mirroring ``durable_activity._activity_epoch``'s
    same fallback for this same field, so plain-dict test fixtures behave like real reads.
    """
    from core.dynamodb import plain

    value = plain(container.get("last_interactive_at"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


async def is_parked_trial(
    owner_id: str,
    container: Mapping[str, Any] | None,
    *,
    account: Mapping[str, Any] | None = None,
) -> bool:
    """The suppress predicate. True ONLY for a healthy, deliberately parked
    trial box.

    Fails OPEN (returns False → legacy wake behavior) on any uncertainty: flag
    off, missing row, non-``stopped`` status, missing/foreign ``stop_reason``,
    a pending conversion (its reprovision owns the row — waking under it is
    the documented EFS-WAL two-task corruption window), a non-trial owner, a
    billing read failure, or (AgentCore only) a missing/unparseable
    ``last_interactive_at``. Self-heal must never depend on this predicate
    being right.
    """
    if not settings.TRIAL_WAKE_GATE_ENABLED:
        return False
    if not container:
        return False

    from core.services.runtime_transport import host_for

    if host_for(container) == "agentcore":
        if not await _is_trial(owner_id, account):
            return False
        last = _last_interactive_epoch(container)
        if last is None:
            return False  # fail OPEN: a fresh row must not show "Agents asleep" pre-first-use
        from core.services.agentcore_runtime import IDLE_TIMEOUT_S

        return (datetime.now(timezone.utc).timestamp() - last) > IDLE_TIMEOUT_S

    if container.get("status") != "stopped":
        return False
    if container.get("stop_reason") != "trial_idle":
        return False
    if container.get("conversion_pending"):
        return False
    return await _is_trial(owner_id, account)


async def legacy_wake_allowed(owner_id: str) -> bool:
    """Debit the legacy poll-wake budget and say whether this wake may proceed.

    Called ONLY when a request with no ``wake`` marker (a pre-gate frontend
    bundle, or any non-browser poller) is about to wake a PARKED row. Bounds
    never-reloaded old tabs to ``TRIAL_WAKE_DAILY_BUDGET`` container starts per
    UTC day. The debit is written first, then judged — an over-budget caller
    still consumed a debit, which keeps the counter an honest record of
    attempts.
    """
    if not settings.TRIAL_WAKE_GATE_ENABLED:
        return True
    from core.repositories import container_repo

    try:
        count = await container_repo.record_wake_debit(owner_id, _utc_today())
    except Exception:
        logger.exception("wake_policy debit failed owner=%s — allowing wake", owner_id)
        return True
    return count <= settings.TRIAL_WAKE_DAILY_BUDGET


def note_wake_dispatched(*, door: str, intent: str) -> None:
    """Emit the dispatch metric — call at EVERY door that starts a container
    (poll, legacy poll, wake endpoint, chat send, teams, recover, provision,
    retry, admin), so no wake is unattributable."""
    try:
        put_metric("container.wake.dispatch", dimensions={"door": door, "intent": intent})
    except Exception:
        logger.debug("wake dispatch metric emission failed", exc_info=True)


def note_wake_suppressed(
    owner_id: str,
    container: Mapping[str, Any] | None,
    *,
    door: str,
    reason: str,
) -> None:
    """Emit the suppressed metric once per (owner, park episode, door, reason)."""
    episode = str((container or {}).get("parked_at") or "")
    key = f"{owner_id}:{door}:{reason}"
    if _suppressed_emitted.get(key) == episode:
        return
    _suppressed_emitted[key] = episode
    try:
        put_metric("container.wake.suppressed", dimensions={"door": door, "reason": reason})
    except Exception:
        logger.debug("wake suppressed metric emission failed", exc_info=True)
