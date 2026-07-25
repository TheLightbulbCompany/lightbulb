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
  ``bedrock_revoked`` un-revokes (``restore_owner_bedrock``) after ONE
  confirming re-read. This is the pull-based re-arm: Metronome's
  payment-gated top-up releases credits with NO webhook to us, so the next
  gate read after a paid top-up is the event that restores Bedrock access.
- A refresh that comes back NON-POSITIVE re-asserts the revoke
  (``enforce_on_low_balance``): the low-balance webhook is one-shot and can
  be lost, so every gate read doubles as the re-enforce loop.
"""

from __future__ import annotations

import logging

import httpx

from core.billing.metronome_fast_gate import MetronomeFastGate, is_stale
from core.billing.metronome_units import cents_to_microcents
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services.bedrock_enforcement import enforce_on_low_balance

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

    refreshed = await _refresh_from_metronome(owner_id, gate)
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


async def read_net_balance_microcents(owner_id: str) -> int | None:
    """One live ``getNetBalance`` read for the owner, in microcents.

    Returns None when the owner has no Metronome customer or the response
    carries no balance number. Raises on transport/HTTP failure — each
    caller picks its own fallback (the gate falls back to the cached row,
    the balance endpoint degrades to null).
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    customer_id = (account or {}).get("metronome_customer_id")
    if not customer_id:
        return None

    from core.billing import metronome_client

    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=5.0) as http:
        data = await metronome_client.for_settings(http).get_net_balance(customer_id=customer_id)
    # Live-verified response shape (sandbox, 2026-06-10):
    #   {"data": {"balance": <number-in-CENTS>, "credit_type_id": …}}
    # The client returns the ``data`` sub-object, so the number lives under
    # "balance" — NOT "net_balance". Metronome's USD credit type is
    # cents-denominated (see core/billing/metronome_units.py); the gate and
    # the API surface speak microcents.
    balance_cents = data.get("balance")
    if balance_cents is None:
        return None
    return cents_to_microcents(float(balance_cents))


async def apply_fresh_balance(
    owner_id: str, remaining_microcents: int, *, gate: MetronomeFastGate | None = None
) -> None:
    """The single "a fresh Metronome balance just arrived" hook.

    Shared by the gate's lazy refresh and the credits-balance endpoint
    write-through, so the cache rewrite, the enforcement re-assert, and the
    pull-based restore can never diverge between the two read paths.
    Best-effort throughout — must never raise into a gate read or the
    balance endpoint.

    It is also where the Paperclip budget fence
    (``core/services/paperclip_budget_fence.py``) is reconciled for the paid
    Bedrock cohort. This hook is the ONE place revoke and restore already
    share a chokepoint, and — unlike the ``/container/status`` tick — it is
    driven by the Metronome low-balance webhook and this module's own lazy
    refresh, so it fires with no user present. That is what stops a
    credits-exhausted owner's routines from storming overnight.
    """
    if gate is None:
        gate = MetronomeFastGate()
    try:
        await gate.set_balance(owner_id, remaining_microcents=remaining_microcents)
    except Exception:
        # The fresh number still answers THIS read; the row rewrite is only
        # the cache for the next one.
        logger.warning("Fast-gate rewrite failed for owner %s after refresh", owner_id, exc_info=True)

    # Dispatched BEFORE the branch so both directions (drained -> fence,
    # topped-up -> lift) get one tick. The fence re-reads the fast-gate row
    # we just wrote, so it sees this balance, and it self-gates on
    # provider_choice — an OAuth/BYOK owner with a stray Metronome customer
    # is never fenced here.
    from core.services.paperclip_budget_fence import schedule_reconcile

    schedule_reconcile(owner_id)

    if remaining_microcents > 0:
        await _maybe_restore_bedrock(owner_id)
        return

    # Re-assert the revoke on every fresh non-positive read. The low-balance
    # webhook is a ONE-SHOT signal that can be eaten (stale-skip racing a
    # redelivery) or undone (a stale-positive refresh restoring a
    # truly-drained owner), and a blocked owner emits no usage so no
    # corrective webhook ever comes — the gate's own reads are the only
    # recurring trigger left. enforce_on_low_balance is idempotent on the
    # bedrock_revoked stamp, so the common already-revoked case costs one
    # DDB read.
    try:
        await enforce_on_low_balance(owner_id, remaining_microcents)
    except Exception:
        put_metric("billing.bedrock_revoke.error")
        logger.exception(
            "Re-assert revoke failed for owner %s — the fast-gate row already blocks new sends",
            owner_id,
        )


async def _refresh_from_metronome(owner_id: str, gate: MetronomeFastGate) -> int | None:
    """Pull ``getNetBalance`` and run the shared fresh-balance hook (cache
    rewrite + enforcement re-assert + pull-based restore). Returns the
    refreshed remaining (microcents), or None when no refresh was possible.
    """
    try:
        remaining_microcents = await read_net_balance_microcents(owner_id)
    except Exception:
        put_metric("metronome.gate.refresh_error")
        logger.warning(
            "Metronome gate refresh failed for owner %s — using cached fast-gate row",
            owner_id,
            exc_info=True,
        )
        return None
    if remaining_microcents is None:
        return None

    await apply_fresh_balance(owner_id, remaining_microcents, gate=gate)
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
        # Confirm with ONE fresh getNetBalance before un-revoking.
        # getNetBalance is async with no SLA, so a single stale-positive
        # read against a truly-drained owner would restore Bedrock AND
        # permanently disarm the one-shot low-balance alert (it already
        # fired for this crossing) — nothing would ever re-revoke. Only a
        # fresh read that still says > 0 may restore.
        confirmed = await read_net_balance_microcents(owner_id)
        if confirmed is None or confirmed <= 0:
            put_metric("billing.bedrock_restore.confirm_failed")
            logger.warning(
                "Bedrock restore for owner %s aborted — confirming balance re-read came back %s",
                owner_id,
                confirmed,
            )
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
