"""Stripe-payment-gated Metronome funding.

A top-up Stripe Checkout payment funds a Metronome PREPAID commit carrying
payment_gate_config STRIPE, so credits release only once Stripe confirms the
payment (Metronome voids the commit on payment failure). Called from the
Stripe webhook (already deduped by event.id), so this function does not
re-dedup; it stamps the Stripe session id into the commit metadata for audit.
"""

from __future__ import annotations

import os

import boto3
import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.config import settings

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
        return
    await client.edit_contract_add_commits(
        metronome_customer_id=customer_id,
        contract_id=contract_id,
        add_commits=[commit],
    )
