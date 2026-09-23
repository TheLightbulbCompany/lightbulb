"""Synchronous credits gate over the Metronome fast-gate projection.

The chat gate (``chat_gate.gate_chat``) and the provision gate both
ask one question — "is this credit-drawing owner out of credits?" — and
must answer it without a synchronous Metronome round-trip on every message
(Metronome's balance read has no low-latency SLA). Both read THIS helper so
they can never disagree about blocked-ness.

Read model:
- The fast-gate DDB row (fed by the low-balance webhook) is the cached
  answer. ``remaining_microcents <= RESERVE_ESTIMATE_MICROCENTS`` blocks —
  NOT ``<= 0`` — mirroring the bedrock-gate's own admission rule (see
  ``_row_blocked``); a MISSING row does not block ON ITS OWN — the ledger
  check below decides that case, since "projection not written yet" and "no
  ledger at all" look identical here but mean opposite things.
- An owner with no COMPLETE ledger (Metronome customer AND contract) ⇒ BLOCK.
  Nothing can be debited and no balance can exist, so admitting is unmetered
  spend on our account, not leniency. Only reachable for credit-drawing
  (``openrouter_managed``) owners, and never for the free tier (``gate_chat`` returns before Layer 3
  for trial_local; ``provision_gate`` short-circuits them earlier still).
- When the row is stale (older than ``METRONOME_GATE_STALENESS_S``) and the
  owner actually has a Metronome customer, the gate lazily refreshes from
  ``getNetBalance`` and rewrites the row. Metronome-down ⇒ the stale row is
  used as-is — the gate must never 500 (or block) on a vendor outage.
  Metronome's payment-gated top-up releases credits with NO webhook to us, so
  the next refresh after a paid top-up is what unblocks the owner.
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

# The paid-balance floor: a balance at or below one request's reservation is
# already unspendable, so every door that predicts admission blocks there.
RESERVE_ESTIMATE_MICROCENTS = 300_000


async def is_owner_blocked(owner_id: str) -> bool:
    """True when the owner is out of spendable managed-inference credits.

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
        return refreshed <= RESERVE_ESTIMATE_MICROCENTS

    # Refresh unavailable. Two situations, opposite answers:
    #
    #   no COMPLETE ledger -> BLOCK. Nothing can be debited and no balance can
    #     exist, so admitting is unmetered spend on our account rather than
    #     leniency. Every caller has already established a credit-drawing
    #     provider_choice, and `gate_chat` now returns before Layer 3 for
    #     trial_local owners, so this cannot reach the free tier.
    #
    #   ledger exists, Metronome unreachable -> stale row. A vendor outage must
    #     never lock out a customer who has already paid.
    if not await _has_complete_metronome_ledger(owner_id):
        put_metric("metronome.gate.blocked_unprovisioned")
        logger.warning(
            "Owner %s draws credits with no complete Metronome ledger — blocking. "
            "ensure_user_ready provisions this BEFORE the gate, and the top-up path "
            "re-ensures, so a persistent block here is a provisioning failure to chase.",
            owner_id,
        )
        return True
    return _row_blocked(row)


async def _has_complete_metronome_ledger(owner_id: str) -> bool:
    """Whether the owner has a ledger that can actually hold credits.

    Customer AND contract, not customer alone. When the Stripe-link step
    fails, ``metronome_provisioning._provision`` deliberately persists ONLY
    ``metronome_customer_id`` and defers the contract (its billing config is
    create-time-only, so creating it unlinked would permanently strand the
    owner's top-ups). Without a contract there are no prepaid commits, so no
    balance can exist and ``grant_prepaid_credits`` refuses outright —
    testing the customer id alone would admit an owner whose ledger cannot
    hold the credits they are spending, in exactly the partial-provisioning
    failure this predicate exists to catch.

    Shares its DEFINITION of a complete ledger with the bedrock-gate Lambda's
    ``paidReserve`` but deliberately NOT its evaluation order. ``paidReserve``
    checks on every path, including when it has a cached projection, because
    the billing row is already in hand there (core.mjs fetches it for
    resolvePlan) so the check is free — and because it is the actual money
    fence: cron- and channel-triggered runs reach Bedrock through it with no
    backend involvement. Here the equivalent lookup is an extra DynamoDB read,
    and ``is_owner_blocked`` runs on every chat message, so it stays on the
    cold path only (stale row AND refresh unavailable). The consequence is
    narrow and accepted: a FRESH positive projection whose ledger has since
    been wiped would admit at this gate for up to
    ``METRONOME_GATE_STALENESS_S``. It would still be denied at the Lambda,
    which is where the spend actually happens.

    Deliberately a separate read rather than a signal threaded out of
    ``read_net_balance_microcents``: this runs only on the cold path (stale
    row AND refresh unavailable), and for an owner in that state the answer
    is about to block them, so the read is self-limiting. A DDB failure
    reports "complete" so a transient blip degrades to the stale-row fallback
    rather than locking out a paying customer.
    """
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
    except Exception:
        logger.exception("Billing read failed while checking Metronome provisioning for owner %s", owner_id)
        return True
    account = account or {}
    return bool(account.get("metronome_customer_id") and account.get("metronome_contract_id"))


def _row_blocked(row: dict | None) -> bool:
    if not row:
        return False
    remaining = row.get("remaining_microcents")
    if remaining is None:
        return bool(row.get("blocked", False))
    # The gate admits only while `remaining - reserved - estimate > 0`, so a
    # balance at or below one reservation is already unspendable. Matching it
    # here keeps the banner, the chat gate and the provision gate agreeing with
    # the only component that actually decides.
    return int(remaining) <= RESERVE_ESTIMATE_MICROCENTS


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
    """The single "a fresh Metronome balance just arrived" hook: rewrite the fast-gate row.

    Shared by the gate's lazy refresh and the credits-balance endpoint write-through.
    Best-effort — must never raise into a gate read or the balance endpoint.
    """
    if gate is None:
        gate = MetronomeFastGate()
    try:
        await gate.set_balance(owner_id, remaining_microcents=remaining_microcents)
    except Exception:
        # The fresh number still answers THIS read; the row rewrite is only
        # the cache for the next one.
        logger.warning("Fast-gate rewrite failed for owner %s after refresh", owner_id, exc_info=True)


async def _refresh_from_metronome(owner_id: str, gate: MetronomeFastGate) -> int | None:
    """Pull ``getNetBalance`` and run the shared fresh-balance hook (cache
    rewrite). Returns the
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
