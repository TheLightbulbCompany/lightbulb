"""Parked-trial policy for an AgentCore workspace — the single predicate.

AWS parks an idle microVM invisibly and wakes it on the next invoke, so a
workspace row has no ``stopped`` status to read: it stays ``status="running"``
forever (report 04 risk 2). The only honest park signal left is elapsed time
since ``last_interactive_at`` against the runtime's own idle timeout
(``agentcore_runtime.IDLE_TIMEOUT_S``), gated to trial owners.

It is a REPORT, not a gate. There is no scaled-to-zero service left for a
passive read to start by accident, so nothing here suppresses a wake any more;
the one remaining consumer that acts on it is ``ensure_user_ready``, which
declines to dispatch a provision for a workspace that is merely idle. Every
public entry point returns "not parked" / no-op when ``TRIAL_WAKE_GATE_ENABLED``
is off, so the CDK flag still restores the pre-gate reporting wholesale.
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
    """The parked predicate. True ONLY for a trial workspace whose microVM AWS has
    had long enough to park.

    Fails OPEN (returns False) on any uncertainty: flag off, missing row, a
    non-trial owner, a billing read failure, or a missing/unparseable
    ``last_interactive_at``. Self-heal must never depend on this predicate being
    right.
    """
    if not settings.TRIAL_WAKE_GATE_ENABLED:
        return False
    if not container:
        return False
    if not await _is_trial(owner_id, account):
        return False
    last = _last_interactive_epoch(container)
    if last is None:
        return False  # fail OPEN: a fresh row must not show "Agents asleep" pre-first-use
    from core.services.agentcore_runtime import IDLE_TIMEOUT_S

    return (datetime.now(timezone.utc).timestamp() - last) > IDLE_TIMEOUT_S


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
