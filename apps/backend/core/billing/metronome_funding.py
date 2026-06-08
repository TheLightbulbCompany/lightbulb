"""Stripe-payment-gated Metronome funding.

A top-up Stripe Checkout payment funds a Metronome PREPAID commit carrying
payment_gate_config STRIPE, so credits release only once Stripe confirms the
payment (Metronome voids the commit on payment failure). Called from the
Stripe webhook (already deduped by event.id), so this function does not
re-dedup; it stamps the Stripe session id into the commit metadata for audit.

After the commit lands it optimistically re-arms the local fast-gate cache
(best-effort) so a previously-out-of-credits owner unblocks on their next
message — the synchronous gate reads the cache, and the low-balance webhook
never fires on a top-up.
"""

from __future__ import annotations

import logging
import os

import boto3
import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings

logger = logging.getLogger(__name__)

_MICROCENTS_PER_DOLLAR = 1_000_000


class FundingError(Exception):
    pass


def _fast_gate_table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — funding is misconfigured.")
    return name


def _customer_id_for(owner_id: str) -> str:
    table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_fast_gate_table_name())
    item = table.get_item(Key={"owner_id": owner_id}, ConsistentRead=True).get("Item")
    customer_id = (item or {}).get("metronome_customer_id")
    if not customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    return customer_id


async def fund_top_up(
    *,
    owner_id: str,
    amount_microcents: int,
    contract_id: str,
    product_id: str,
    stripe_session_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Add a Stripe-payment-gated prepaid commit for amount_microcents."""
    if amount_microcents <= 0:
        raise FundingError(f"amount_microcents must be positive, got {amount_microcents}")

    customer_id = _customer_id_for(owner_id)
    commit = {
        "product_id": product_id,
        "amount": amount_microcents / _MICROCENTS_PER_DOLLAR,
        "payment_gate_config": {"payment_gate_type": "STRIPE"},
        "metadata": {"stripe_session_id": stripe_session_id, "owner_id": owner_id},
    }

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await metronome_client.for_settings(http).edit_contract_add_commits(
                metronome_customer_id=customer_id,
                contract_id=contract_id,
                add_commits=[commit],
            )
    else:
        await client.edit_contract_add_commits(
            metronome_customer_id=customer_id,
            contract_id=contract_id,
            add_commits=[commit],
        )

    # Optimistically re-arm the local fast-gate cache so the owner's NEXT chat
    # message unblocks immediately. get_balance reads the cache (not Metronome's
    # async getNetBalance), and the low-balance webhook only fires when balance
    # crosses DOWN past a threshold — never on a top-up — so without this re-arm
    # a paid top-up would never unblock a previously-out-of-credits owner
    # (spec §3.3: "fund Metronome prepaid credit -> flag refreshed"). Safe
    # direction: the commit above already landed against a CONFIRMED Stripe
    # payment. Best-effort — the commit is what releases money; if the cache
    # write fails the next low-balance webhook re-syncs the authoritative
    # number, so we must not raise (that would 500 the webhook back to Stripe
    # AFTER the commit succeeded and trigger a redelivery storm / double commit).
    try:
        await MetronomeFastGate().add_credit(owner_id, microcents=amount_microcents)
    except Exception:
        logger.exception(
            "Metronome fast-gate re-arm failed after funding owner %s session %s "
            "(commit landed; next low-balance webhook will re-sync)",
            owner_id,
            stripe_session_id,
        )


# Metronome documented auto-recharge minimums (research §2): the trigger
# threshold must be at least $5, and the recharge-to target must exceed the
# threshold by at least $10.
_MIN_THRESHOLD_CENTS = 500
_MIN_RECHARGE_OVER_THRESHOLD_CENTS = 1000


async def set_auto_recharge(
    *,
    owner_id: str,
    enabled: bool,
    threshold_cents: int | None = None,
    amount_cents: int | None = None,
    contract_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Configure Metronome auto-recharge for an owner (replaces the legacy
    DDB ledger's set_auto_reload).

    Maps the legacy (threshold_cents, amount_cents-as-increment) contract onto
    Metronome's credit_balance_threshold_configuration, where recharge_to_amount
    is a TARGET balance, not an increment: recharge_to = threshold + increment.
    Enforces Metronome's documented minimums (research §2). Unlike fund_top_up's
    best-effort re-arm, this is a user-initiated settings write, so failures
    raise FundingError for the router to surface.
    """
    customer_id = _customer_id_for(owner_id)

    if not enabled:
        await _set_threshold(customer_id, contract_id, is_enabled=False, client=client)
        return

    if threshold_cents is None or amount_cents is None:
        raise FundingError("threshold_cents and amount_cents are required when enabling auto-recharge")
    if threshold_cents < _MIN_THRESHOLD_CENTS:
        raise FundingError(f"threshold must be at least ${_MIN_THRESHOLD_CENTS / 100:.0f}")
    if amount_cents < _MIN_RECHARGE_OVER_THRESHOLD_CENTS:
        raise FundingError(
            f"recharge increment must be at least ${_MIN_RECHARGE_OVER_THRESHOLD_CENTS / 100:.0f} "
            "(Metronome requires recharge_to >= threshold + $10)"
        )

    threshold_dollars = threshold_cents / 100
    recharge_to_dollars = (threshold_cents + amount_cents) / 100
    await _set_threshold(
        customer_id,
        contract_id,
        is_enabled=True,
        threshold_amount=threshold_dollars,
        recharge_to_amount=recharge_to_dollars,
        client=client,
    )


async def _set_threshold(
    customer_id: str,
    contract_id: str,
    *,
    is_enabled: bool,
    threshold_amount: float | None = None,
    recharge_to_amount: float | None = None,
    client: MetronomeClient | None = None,
) -> None:
    if client is not None:
        await client.set_credit_balance_threshold(
            metronome_customer_id=customer_id,
            contract_id=contract_id,
            is_enabled=is_enabled,
            threshold_amount=threshold_amount,
            recharge_to_amount=recharge_to_amount,
        )
        return
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
        await metronome_client.for_settings(http).set_credit_balance_threshold(
            metronome_customer_id=customer_id,
            contract_id=contract_id,
            is_enabled=is_enabled,
            threshold_amount=threshold_amount,
            recharge_to_amount=recharge_to_amount,
        )
