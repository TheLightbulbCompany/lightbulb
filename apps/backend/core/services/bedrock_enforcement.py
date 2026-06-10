"""Bedrock per-owner enforcement orchestrator: balance-crossed-zero policy.

This module owns the WHEN of the kill-switch; the IAM kill-switch PRIMITIVES
it relies on (`revoke_owner_bedrock` / `restore_owner_bedrock`) plus the AIP
resolvers (`resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn`) live in
`core/billing/bedrock_aip.py` and are imported lazily on the kill path —
never redefined here. Two entry points share one fire implementation:

  * `enforce_after_deplete` — the interim-DDB-ledger trigger (still wired
    while the ledger exists).
  * `enforce_on_low_balance` — the Metronome low-balance-webhook trigger,
    additionally gated on settings.BEDROCK_ENFORCEMENT_LIVE so the Metronome
    pipeline can run in shadow (flag set, no kill) until armed.
"""

from __future__ import annotations

import logging

from core.billing.ledger_interface import DepleteResult
from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


async def enforce_after_deplete(owner_id: str, result: DepleteResult) -> None:
    """React to one ledger deplete: fire the kill-switch when this event was
    newly applied AND the resulting balance is non-positive.

    Called by `POST /internal/usage/from-bedrock-log` right after
    `get_ledger().deplete(event)`. A deduplicated replay
    (``applied is False``) is a no-op — it must not thrash ECS on every DLQ
    re-delivery — and a still-positive balance is a no-op too.
    """
    if not result.applied or result.balance_microcents > 0:
        return
    await _fire_kill_switch(owner_id, balance_microcents=result.balance_microcents)


async def enforce_on_low_balance(owner_id: str, remaining_microcents: int) -> None:
    """React to a Metronome low-balance webhook: fire the kill-switch when the
    reported remaining balance is non-positive AND enforcement is armed.

    With BEDROCK_ENFORCEMENT_LIVE off (the default) this is a pure no-op —
    the webhook handler still projects the balance onto the fast-gate flag,
    so the whole Metronome pipeline runs in shadow with zero blast radius.
    """
    if remaining_microcents > 0 or not settings.BEDROCK_ENFORCEMENT_LIVE:
        return
    await _fire_kill_switch(owner_id, balance_microcents=remaining_microcents)


async def _fire_kill_switch(owner_id: str, *, balance_microcents: int) -> None:
    """Shared fire path. The kill is best-effort: an ECS/IAM failure is
    logged, not raised, because the spend is already incurred and the next
    trigger (or the provision gate's balance read) re-asserts the stop.

    `get_ecs_manager` is imported lazily inside the function (after the
    callers' guards) to avoid eagerly pulling the container-orchestration
    import tree into this module.
    """
    from core.containers import get_ecs_manager

    manager = get_ecs_manager()
    # Short-circuit repeated fires during the IAM-propagation window. The
    # FIRST trigger crossing <= 0 fires the kill; subsequent triggers
    # arriving in the ~5-15s before IAM/ECS propagate would ALSO see
    # balance <= 0 and re-fire. ECS desired=0 is idempotent, but re-firing
    # means repeated IAM revoke calls + metric noise. If the service is
    # already stopped, skip the redundant kill.
    if await manager.is_owner_service_stopped(owner_id):
        logger.info(
            "Owner %s already stopped — skipping redundant kill-switch fire",
            owner_id,
        )
        return

    put_metric(
        "billing.killswitch.fire",
        dimensions={"reason": "balance_non_positive"},
    )
    logger.warning(
        "Owner %s crossed balance<=0 (%d microcents) — firing Bedrock kill-switch",
        owner_id,
        balance_microcents,
    )
    try:
        await manager.kill_owner_bedrock(owner_id)
    except Exception:
        put_metric("billing.killswitch.error")
        logger.exception(
            "Kill-switch failed for owner %s — spend already incurred; the "
            "provision gate keeps the container down until balance recovers",
            owner_id,
        )
