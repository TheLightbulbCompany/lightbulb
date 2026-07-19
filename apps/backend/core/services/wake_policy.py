"""Wake-on-intent policy for parked trial containers — the single predicate.

A container is PARKED iff its row says ``status="stopped"`` AND
``stop_reason == "trial_idle"`` (stamped only by the trial teardown ladder) AND
the owner is still ``trial_local`` AND no trial→paid conversion is pending.
Parked is the ONLY state whose wake the status poll may suppress; every other
stopped/error shape keeps its poll-driven self-heal exactly as before (crashed
boxes, killswitch stops, stale provision leases, converting rows).

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
    the documented EFS-WAL two-task corruption window), a non-trial owner, or
    a billing read failure. Self-heal must never depend on this predicate
    being right.
    """
    if not settings.TRIAL_WAKE_GATE_ENABLED:
        return False
    if not container or container.get("status") != "stopped":
        return False
    if container.get("stop_reason") != "trial_idle":
        return False
    if container.get("conversion_pending"):
        return False
    from core.repositories import billing_repo

    try:
        acct = account if account is not None else await billing_repo.get_by_owner_id(owner_id)
    except Exception:
        logger.exception("wake_policy billing read failed owner=%s — treating as not parked", owner_id)
        return False
    return bool(acct) and acct.get("subscription_status") == "trial_local"


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
