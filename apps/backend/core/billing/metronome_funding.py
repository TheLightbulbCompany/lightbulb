"""Metronome-native top-up funding: PAYMENT-GATED prepaid commits.

A top-up request creates a Metronome PREPAID commit with
``payment_gate_config: {"payment_gate_type": "STRIPE"}``. Metronome invoices
the customer's linked Stripe card (``stripe_collection_method:
"charge_automatically"``, set at provision via the billing-provider
configuration) and releases the credits only when Stripe collects — the
backend never touches the money and writes NO local balance. There is no
payment-success webhook to us either: the gate's lazy balance refresh
(core/services/metronome_gate.py) is what observes the released credits and
un-revokes a previously-depleted owner.
"""

from __future__ import annotations

import logging
import time

import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# The body Metronome's payment gate expects on the commit. STRIPE means
# Metronome itself runs the Stripe payment attempt against the customer's
# billing-provider configuration and releases the commit on success.
_STRIPE_PAYMENT_GATE = {"payment_gate_type": "STRIPE"}


class FundingError(Exception):
    pass


async def request_top_up(
    owner_id: str,
    amount_cents: int,
    *,
    client: MetronomeClient | None = None,
) -> None:
    """Create a payment-gated prepaid commit for ``amount_cents``.

    The Metronome customer UUID + contract id live on the billing row
    (persisted by ensure_metronome_customer at provision time). Raises
    FundingError when the owner isn't Metronome-provisioned or the
    lifecycle switch is off — the caller (the top-up endpoint) surfaces
    that to the user instead of silently dropping a purchase request.

    settings.METRONOME_CREDITS_PRODUCT_ID must reference a Metronome
    product carrying a ``stripe_product_id`` custom field (console setup,
    per env) — Metronome builds the Stripe invoice line item from it, and
    the payment attempt fails without it.
    """
    if not settings.METRONOME_LIFECYCLE_ENABLED:
        raise FundingError("Metronome lifecycle is disabled — top-ups are unavailable")
    if amount_cents <= 0:
        raise FundingError(f"amount_cents must be positive, got {amount_cents}")

    account = await billing_repo.get_by_owner_id(owner_id) or {}
    metronome_customer_id = account.get("metronome_customer_id")
    metronome_contract_id = account.get("metronome_contract_id")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    # The commit body itself (flat customerCommits/create shape) is owned by
    # the client layer; this module supplies the money in CENTS (Metronome's
    # USD credit type — see core/billing/metronome_units.py), the payment
    # gate, and an audit description.
    description = f"isol8 top-up owner={owner_id} requested_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            client = metronome_client.for_settings(http)
            await _create_gated_commit(
                client,
                customer_id=metronome_customer_id,
                contract_id=metronome_contract_id,
                amount_cents=amount_cents,
                description=description,
            )
    else:
        await _create_gated_commit(
            client,
            customer_id=metronome_customer_id,
            contract_id=metronome_contract_id,
            amount_cents=amount_cents,
            description=description,
        )

    logger.info(
        "Requested payment-gated Metronome top-up for owner %s: %d cents",
        owner_id,
        amount_cents,
    )


async def _create_gated_commit(
    client: MetronomeClient,
    *,
    customer_id: str,
    contract_id: str,
    amount_cents: int,
    description: str,
) -> None:
    await client.create_prepaid_commit(
        customer_id=customer_id,
        contract_id=contract_id,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
        credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
        amount_cents=amount_cents,
        description=description,
        payment_gate_config=_STRIPE_PAYMENT_GATE,
    )
