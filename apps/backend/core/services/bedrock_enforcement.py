"""Bedrock per-owner enforcement orchestrator: the post-deplete policy.

Plan 4 owns this module — the post-deplete enforcement policy
(`enforce_after_deplete`). The IAM kill-switch PRIMITIVES it relies on
(`revoke_owner_bedrock` / `restore_owner_bedrock`) plus the AIP resolvers
(`resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn`) live in
`core/billing/bedrock_aip.py`
(owned by Plan 2) and are imported lazily on the kill path — never
redefined here. This module is the orchestrator; `bedrock_aip` is the
primitive layer.
"""

from __future__ import annotations

import logging

from core.billing.ledger_interface import DepleteResult
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


async def enforce_after_deplete(owner_id: str, result: DepleteResult) -> None:
    """React to one ledger deplete: fire the kill-switch when this event was
    newly applied AND the resulting balance is non-positive.

    Called by Plan 3's `POST /internal/usage/from-bedrock-log` endpoint right
    after `get_ledger().deplete(event)`. A deduplicated replay
    (``applied is False``) is a no-op — it must not thrash ECS on every DLQ
    re-delivery — and a still-positive balance is a no-op too. The kill is
    best-effort: an ECS/IAM failure is logged, not raised, because the spend
    is already incurred and the next deplete (or the provision gate's
    balance read) re-asserts the stop.

    `get_ecs_manager` is imported lazily inside the function (after the guard)
    to avoid eagerly pulling the container-orchestration import tree into this
    module — see the import-cycle note above.
    """
    if not result.applied or result.balance_microcents > 0:
        return

    from core.containers import get_ecs_manager

    manager = get_ecs_manager()
    # Short-circuit repeated fires during the IAM-propagation window. The
    # FIRST applied deplete crossing <= 0 fires the kill; subsequent applied
    # deplete()s for NEW requestIds arriving in the ~5-15s before IAM/ECS
    # propagate would ALSO have balance <= 0 and re-fire. ECS desired=0 is
    # idempotent, but re-firing means repeated IAM revoke calls + metric
    # noise. If the service is already stopped, skip the redundant kill.
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
        result.balance_microcents,
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
