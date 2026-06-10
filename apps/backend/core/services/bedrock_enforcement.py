"""Bedrock per-owner enforcement orchestrator: balance-crossed-zero policy.

Enforcement is REVOKE-ONLY. The $50 subscription pays for the container;
prepaid credits pay for Bedrock inference. So when an owner's balance
crosses zero we remove the per-owner role's scoped Bedrock inline policy
(`revoke_owner_bedrock` from `core/billing/bedrock_aip.py`) — every
subsequent invoke fails AccessDenied before a single token is generated —
and we deliberately do NOT touch the container. It stays up serving the
other providers (ChatGPT OAuth / BYO key) and channels the owner already
paid for. The combined revoke+desired=0 kill
(`EcsManager.kill_owner_bedrock`) remains available as an admin break-glass
for genuine runaways, never as balance enforcement.

The single trigger is `enforce_on_low_balance` — the Metronome
low-balance-webhook entry point, gated on settings.BEDROCK_ENFORCEMENT_LIVE
so the Metronome pipeline can run in shadow (flag set, no revoke) until
armed. Usage ingestion never touches the backend (the meter Lambda sends
straight to Metronome), so there is no per-deplete trigger anymore. The
reverse transition (restore on a paid top-up) is pull-based: the gate's
lazy balance refresh un-revokes when it sees balance > 0 on a revoked
owner — see core/services/metronome_gate.py.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


async def enforce_on_low_balance(owner_id: str, remaining_microcents: int) -> None:
    """React to a Metronome low-balance webhook: revoke the owner's Bedrock
    IAM grant when the reported remaining balance is non-positive AND
    enforcement is armed.

    With BEDROCK_ENFORCEMENT_LIVE off this is a pure no-op — the webhook
    handler still projects the balance onto the fast-gate flag, so the whole
    Metronome pipeline runs in shadow with zero blast radius.
    """
    if remaining_microcents > 0 or not settings.BEDROCK_ENFORCEMENT_LIVE:
        return
    await _fire_revoke(owner_id, balance_microcents=remaining_microcents)


async def _fire_revoke(owner_id: str, *, balance_microcents: int) -> None:
    """Revoke the per-owner Bedrock IAM policy. Best-effort: an IAM failure
    is logged, not raised, because the spend is already incurred and the
    next low-balance trigger re-asserts the revoke (the idempotency guard
    below only skips once the revoke actually LANDED and stamped the row).

    Imports are lazy (after the callers' guards) to keep the IAM/billing
    import tree out of this module at import time.
    """
    from core.billing.bedrock_aip import revoke_owner_bedrock
    from core.repositories import container_repo

    # Idempotency guard: revoke_owner_bedrock stamps bedrock_revoked=True on
    # the containers row when it lands. Subsequent low-balance triggers
    # arriving while the balance is still <= 0 would re-fire the same IAM
    # delete; the stamp makes the repeat a cheap DDB read instead. A missing
    # row does NOT skip — revoke_owner_bedrock itself treats a missing IAM
    # policy as already-revoked, so attempting is always safe.
    container = await container_repo.get_by_owner_id(owner_id)
    if container and container.get("bedrock_revoked"):
        logger.info(
            "Owner %s already Bedrock-revoked — skipping redundant revoke fire",
            owner_id,
        )
        return

    put_metric(
        "billing.bedrock_revoke.fire",
        dimensions={"reason": "balance_non_positive"},
    )
    logger.warning(
        "Owner %s crossed balance<=0 (%d microcents) — revoking Bedrock IAM grant",
        owner_id,
        balance_microcents,
    )
    try:
        await revoke_owner_bedrock(owner_id)
    except Exception:
        put_metric("billing.bedrock_revoke.error")
        logger.exception(
            "Bedrock revoke failed for owner %s — spend already incurred; the "
            "fast-gate flag blocks new sends and the next low-balance trigger re-fires",
            owner_id,
        )
