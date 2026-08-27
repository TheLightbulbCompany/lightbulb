"""Metronome-native top-up funding: PAYMENT-GATED prepaid commits.

A top-up request adds a STRIPE-gated PREPAID commit via the v2 contract
edit (the only API surface where payment gating exists — see
MetronomeClient.create_gated_prepaid_commit). Metronome invoices the
customer's linked Stripe card (``stripe_collection_method:
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


class FundingError(Exception):
    pass


async def request_top_up(
    owner_id: str,
    amount_cents: int,
    *,
    idempotency_key: str | None = None,
    client: MetronomeClient | None = None,
) -> None:
    """Create a payment-gated prepaid commit for ``amount_cents``.

    The Metronome customer UUID + contract id live on the billing row
    (persisted by ensure_metronome_customer at provision time). Raises
    FundingError when the owner isn't Metronome-provisioned or the
    lifecycle switch is off — the caller (the top-up endpoint) surfaces
    that to the user instead of silently dropping a purchase request.

    ``idempotency_key`` is the double-charge guard: it rides to Metronome
    as the native Idempotency-Key header, so a replayed request returns
    the original commit instead of creating (and charging) a second one.
    Vendor-side idempotency replaces the old time-windowed dedup row —
    a windowed key had a boundary hole (two clicks straddling the window
    edge both passed), unacceptable for an unconditional card charge.

    settings.METRONOME_CREDITS_PRODUCT_ID must reference a Metronome
    product carrying a ``stripe_product_id`` custom field (console setup,
    per env) — Metronome builds the Stripe invoice line item from it, and
    the payment attempt fails without it.
    """
    if not settings.METRONOME_CREDITS_PRODUCT_ID:
        # An empty product id would reach Metronome as an invalid commit body
        # and surface as an opaque 4xx. Fail loudly at the source instead —
        # the env is mid-runbook, not the user's fault.
        raise FundingError("Metronome credits product is not configured for this environment")
    if amount_cents <= 0:
        raise FundingError(f"amount_cents must be positive, got {amount_cents}")

    account = await billing_repo.get_by_owner_id(owner_id) or {}
    metronome_customer_id = account.get("metronome_customer_id")
    metronome_contract_id = account.get("metronome_contract_id")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    # Fallback for callers that don't carry a client-generated token: a
    # deterministic owner+amount+minute key. NOTE this retains the window-
    # boundary hole (two submits straddling the minute edge get distinct
    # keys, so both charge) — real clients pass a per-intent UUID through
    # ``idempotency_key`` and only get here when one is genuinely absent.
    key = idempotency_key or f"topup-{owner_id}-{amount_cents}-{int(time.time() // 60)}"

    # The commit body itself (v2 contract-edit add_commits shape, gate
    # included) is owned by the client layer; this module supplies the money
    # in CENTS (Metronome's USD credit type — see
    # core/billing/metronome_units.py) — which feeds BOTH the access and
    # invoice schedules 1:1 (credits unlocked == cents invoiced) — and an
    # audit description. The description carries NO timestamp: Metronome's
    # idempotent replay requires the retried body to be identical (same key
    # + different body → 409), and Metronome stamps the commit's creation
    # time itself.
    description = f"lightbulb top-up owner={owner_id} amount_cents={amount_cents}"

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await _create_gated_commit(
                metronome_client.for_settings(http),
                customer_id=metronome_customer_id,
                contract_id=metronome_contract_id,
                amount_cents=amount_cents,
                description=description,
                idempotency_key=key,
            )
    else:
        await _create_gated_commit(
            client,
            customer_id=metronome_customer_id,
            contract_id=metronome_contract_id,
            amount_cents=amount_cents,
            description=description,
            idempotency_key=key,
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
    idempotency_key: str,
) -> None:
    await client.create_gated_prepaid_commit(
        customer_id=customer_id,
        contract_id=contract_id,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
        credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
        amount_cents=amount_cents,
        description=description,
        idempotency_key=idempotency_key,
    )


async def grant_prepaid_credits(
    owner_id: str,
    amount_cents: int,
    *,
    idempotency_key: str,
    client: MetronomeClient | None = None,
) -> None:
    """Grant ``amount_cents`` of prepaid credits for a payment collected
    OUTSIDE Metronome (our own Stripe Checkout).

    Creates an UNGATED prepaid commit — access schedule only, no
    invoice_schedule and no payment gate — which is the API form of
    Metronome's documented "Do not invoice" option for externally-collected
    payments. Live-verified (sandbox, 2026-06-12): the commit releases the
    credits IMMEDIATELY (balance reflects in the same read) and generates
    ZERO Metronome invoices, so the customer is never double-billed for
    money our Checkout already took.

    ``idempotency_key`` is REQUIRED (no fallback): the only caller is the
    checkout webhook, which keys on the Stripe Checkout session id — one
    completed session grants exactly once, however many times Stripe
    redelivers the event.

    Same provisioning guards as :func:`request_top_up` — a FundingError
    here means the owner's Metronome lifecycle isn't set up, which the
    webhook surfaces as a metric + retryable failure rather than silently
    swallowing paid-for credits.
    """
    if not settings.METRONOME_CREDITS_PRODUCT_ID:
        raise FundingError("Metronome credits product is not configured for this environment")
    if amount_cents <= 0:
        raise FundingError(f"amount_cents must be positive, got {amount_cents}")

    account = await billing_repo.get_by_owner_id(owner_id) or {}
    metronome_customer_id = account.get("metronome_customer_id")
    metronome_contract_id = account.get("metronome_contract_id")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    # No timestamp in the description — idempotent replay requires a
    # byte-identical body (same key + different body → 409).
    description = f"lightbulb checkout top-up owner={owner_id} amount_cents={amount_cents}"

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await _grant_ungated_commit(
                metronome_client.for_settings(http),
                customer_id=metronome_customer_id,
                contract_id=metronome_contract_id,
                amount_cents=amount_cents,
                description=description,
                idempotency_key=idempotency_key,
            )
    else:
        await _grant_ungated_commit(
            client,
            customer_id=metronome_customer_id,
            contract_id=metronome_contract_id,
            amount_cents=amount_cents,
            description=description,
            idempotency_key=idempotency_key,
        )

    logger.info(
        "Granted checkout-paid Metronome credits for owner %s: %d cents",
        owner_id,
        amount_cents,
    )


async def _grant_ungated_commit(
    client: MetronomeClient,
    *,
    customer_id: str,
    contract_id: str,
    amount_cents: int,
    description: str,
    idempotency_key: str,
) -> None:
    await client.create_prepaid_commit(
        customer_id=customer_id,
        contract_id=contract_id,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
        credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
        amount_cents=amount_cents,
        description=description,
        idempotency_key=idempotency_key,
    )
