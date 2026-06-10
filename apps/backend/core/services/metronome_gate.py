"""Synchronous credits gate over the Metronome fast-gate projection.

The chat gate (``connection_pool.gate_chat``) and the provision gate both
ask one question — "is this Bedrock-managed owner out of credits?" — and
must answer it without a synchronous Metronome round-trip on every message
(Metronome's balance read has no low-latency SLA). Both read THIS helper so
they can never disagree about blocked-ness.

Read model:
- The fast-gate DDB row (fed by the low-balance webhook) is the cached
  answer. ``remaining_microcents <= 0`` blocks; a MISSING row does NOT
  block (a brand-new owner must not be gated by a missing projection).
- When the row is stale (older than ``METRONOME_GATE_STALENESS_S``) and the
  owner actually has a Metronome customer, the gate lazily refreshes from
  ``getNetBalance`` and rewrites the row. Metronome-down ⇒ the stale row is
  used as-is — the gate must never 500 (or block) on a vendor outage.
- A refresh that comes back POSITIVE for an owner whose containers row says
  ``bedrock_revoked`` un-revokes (``restore_owner_bedrock``). This is the
  pull-based re-arm: Metronome's payment-gated top-up releases credits with
  NO webhook to us, so the next gate read after a paid top-up is the event
  that restores Bedrock access.
"""

from __future__ import annotations

import logging

import httpx

from core.billing.metronome_fast_gate import MetronomeFastGate, is_stale
from core.billing.metronome_units import cents_to_microcents
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo

logger = logging.getLogger(__name__)


async def is_owner_blocked(owner_id: str) -> bool:
    """True when the owner is out of spendable Bedrock credits.

    The single blocked-ness predicate for both the pre-chat gate and the
    provision gate. Never raises on Metronome/DDB trouble in the refresh
    path — the cached row (or, failing that, "not blocked") is the answer.
    """
    gate = MetronomeFastGate()
    row = await gate.get_row(owner_id)
    if not is_stale(row, settings.METRONOME_GATE_STALENESS_S):
        return _row_blocked(row)

    refreshed = await _refresh_from_metronome(owner_id, gate, row)
    if refreshed is not None:
        return refreshed <= 0
    # Refresh unavailable (no Metronome customer, or Metronome down): use
    # the stale row as-is. A missing row stays NOT blocked.
    return _row_blocked(row)


def _row_blocked(row: dict | None) -> bool:
    if not row:
        return False
    remaining = row.get("remaining_microcents")
    if remaining is None:
        return bool(row.get("blocked", False))
    return int(remaining) <= 0


async def _refresh_from_metronome(owner_id: str, gate: MetronomeFastGate, row: dict | None) -> int | None:
    """Pull ``getNetBalance``, rewrite the fast-gate row, and re-arm a
    revoked owner when the fresh balance is positive. Returns the refreshed
    remaining (microcents), or None when no refresh was possible.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    customer_id = (account or {}).get("metronome_customer_id")
    if not customer_id:
        return None

    try:
        from core.billing import metronome_client

        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=5.0) as http:
            data = await metronome_client.for_settings(http).get_net_balance(customer_id=customer_id)
        net_balance = data.get("net_balance")
        if net_balance is None:
            return None
        # getNetBalance reports Metronome's USD credit type in CENTS (see
        # core/billing/metronome_units.py); the gate speaks microcents.
        remaining_microcents = cents_to_microcents(float(net_balance))
    except Exception:
        put_metric("metronome.gate.refresh_error")
        logger.warning(
            "Metronome gate refresh failed for owner %s — using cached fast-gate row",
            owner_id,
            exc_info=True,
        )
        return None

    try:
        await gate.set_balance(owner_id, remaining_microcents=remaining_microcents)
    except Exception:
        # The fresh number still answers THIS gate read; the row rewrite is
        # only the cache for the next one.
        logger.warning("Fast-gate rewrite failed for owner %s after refresh", owner_id, exc_info=True)

    if remaining_microcents > 0:
        await _maybe_restore_bedrock(owner_id)
    return remaining_microcents


async def _maybe_restore_bedrock(owner_id: str) -> None:
    """Pull-based re-arm: un-revoke a paid-up owner. Best-effort — a restore
    failure must not block (or 500) the gate read; the owner stays revoked
    until the next refresh re-attempts. Lazy imports keep the IAM/container
    layer out of this module's import tree.
    """
    from core.repositories import container_repo

    try:
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("bedrock_revoked"):
            return
        from core.billing.bedrock_aip import restore_owner_bedrock

        put_metric("billing.bedrock_restore.fire", dimensions={"reason": "gate_refresh_positive"})
        logger.warning("Owner %s paid up (balance > 0) — restoring Bedrock IAM grant", owner_id)
        await restore_owner_bedrock(owner_id)
    except Exception:
        put_metric("billing.bedrock_restore.error")
        logger.exception(
            "Bedrock restore failed for owner %s — next gate refresh re-attempts",
            owner_id,
        )
