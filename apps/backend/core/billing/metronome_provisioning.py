"""Idempotent Metronome customer provisioning.

Creates a Metronome customer whose ingest_aliases include our owner_id (so
usage events carry customer_id = owner_id, no translation table) and links the
EXISTING Stripe customer so Metronome pushes usage/credit invoices onto the
same Stripe Customer as the $50/mo subscription. The Metronome customer UUID
is stashed on the fast-gate table row so a re-call short-circuits both HTTP
calls.
"""

from __future__ import annotations

import os

import boto3
import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.config import settings


def _fast_gate_table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — provisioning is misconfigured.")
    return name


def _table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_fast_gate_table_name())


def _existing_customer_id(owner_id: str) -> str | None:
    resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
    item = resp.get("Item")
    if not item:
        return None
    return item.get("metronome_customer_id")


async def ensure_metronome_customer(
    *,
    owner_id: str,
    stripe_customer_id: str,
    client: MetronomeClient | None = None,
) -> str:
    """Return the Metronome customer UUID, creating + Stripe-linking on first
    call. Idempotent on the persisted metronome_customer_id."""
    existing = _existing_customer_id(owner_id)
    if existing:
        return existing

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            return await _provision(owner_id, stripe_customer_id, metronome_client.for_settings(http))
    return await _provision(owner_id, stripe_customer_id, client)


async def _provision(owner_id: str, stripe_customer_id: str, client: MetronomeClient) -> str:
    customer_id = await client.create_customer(name=owner_id, ingest_aliases=[owner_id])
    await client.set_stripe_billing_config(
        metronome_customer_id=customer_id,
        stripe_customer_id=stripe_customer_id,
    )
    _table().update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="SET metronome_customer_id = :c",
        ExpressionAttributeValues={":c": customer_id},
    )
    return customer_id
