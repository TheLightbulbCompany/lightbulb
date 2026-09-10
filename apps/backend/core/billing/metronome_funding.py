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

# Metronome retains REST Idempotency-Key results for at least 24 hours. Stop
# automated replays one hour early so clock/network skew cannot cross that
# guarantee and turn the same key into a second provider write.
_METRONOME_RETRY_WINDOW_SECONDS = 23 * 60 * 60


class FundingError(Exception):
    pass


async def request_top_up(
    owner_id: str,
    amount_cents: int,
    *,
    request_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Create a payment-gated prepaid commit for ``amount_cents``.

    The Metronome customer UUID + contract id live on the billing row
    (persisted by ensure_metronome_customer at provision time). Raises
    FundingError when the owner isn't Metronome-provisioned or the
    lifecycle switch is off — the caller (the top-up endpoint) surfaces
    that to the user instead of silently dropping a purchase request.

    ``request_id`` is a caller UUID identifying one purchase intent. Its
    workspace-scoped permanent record pins the Metronome request body and
    native Idempotency-Key, including the first request's UTC midnight.

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

    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if account.get("subscription_status") != "active" or account.get("plan", "standard") not in {
        "standard",
        "ultra_pro",
    }:
        raise FundingError("An active Workspace or Ultra subscription is required")
    stripe_customer_id = account.get("stripe_customer_id")
    if not stripe_customer_id:
        raise FundingError(f"Stripe customer not provisioned for owner_id={owner_id}")
    metronome_customer_id = account.get("metronome_customer_id")
    metronome_contract_id = account.get("metronome_contract_id")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await _request_top_up(
                metronome_client.for_settings(http),
                owner_id=owner_id,
                request_id=request_id,
                amount_cents=amount_cents,
                stripe_customer_id=stripe_customer_id,
                metronome_customer_id=metronome_customer_id,
                metronome_contract_id=metronome_contract_id,
            )
    else:
        await _request_top_up(
            client,
            owner_id=owner_id,
            request_id=request_id,
            amount_cents=amount_cents,
            stripe_customer_id=stripe_customer_id,
            metronome_customer_id=metronome_customer_id,
            metronome_contract_id=metronome_contract_id,
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
    effective_at: str,
) -> None:
    await client.create_gated_prepaid_commit(
        customer_id=customer_id,
        contract_id=contract_id,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
        credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
        amount_cents=amount_cents,
        description=description,
        idempotency_key=idempotency_key,
        effective_at=effective_at,
    )


def _is_active_stripe_link(link: object, stripe_customer_id: str) -> bool:
    if not isinstance(link, dict) or link.get("billing_provider") != "stripe" or link.get("archived_at") is not None:
        return False
    configuration = link.get("configuration")
    return (
        isinstance(configuration, dict)
        and configuration.get("stripe_customer_id") == stripe_customer_id
        and configuration.get("stripe_collection_method") == "charge_automatically"
    )


async def _verify_workspace_stripe_link(
    client: MetronomeClient,
    *,
    stripe_customer_id: str,
    metronome_customer_id: str,
    metronome_contract_id: str,
) -> None:
    root_links = await client.get_customer_billing_provider_configurations(customer_id=metronome_customer_id)
    if not isinstance(root_links, list) or not any(
        _is_active_stripe_link(link, stripe_customer_id) for link in root_links
    ):
        raise FundingError("Metronome Stripe billing link is unavailable for this workspace")
    contract = await client.get_contract(customer_id=metronome_customer_id, contract_id=metronome_contract_id)
    if (
        not isinstance(contract, dict)
        or contract.get("customer_id") != metronome_customer_id
        or not _is_active_stripe_link(contract.get("customer_billing_provider_configuration"), stripe_customer_id)
    ):
        raise FundingError("Metronome Stripe billing link is unavailable for this workspace")


async def _request_top_up(
    client: MetronomeClient,
    *,
    owner_id: str,
    request_id: str,
    amount_cents: int,
    stripe_customer_id: str,
    metronome_customer_id: str,
    metronome_contract_id: str,
) -> None:
    from core.services.webhook_dedup import (
        TopUpIntentConflict,
        complete_top_up_intent,
        get_or_create_top_up_intent,
        record_top_up_provider_attempt,
        set_active_top_up_status,
    )

    binding = {
        "amount_cents": amount_cents,
        "effective_at": time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime()),
        "metronome_customer_id": metronome_customer_id,
        "metronome_contract_id": metronome_contract_id,
        "stripe_customer_id": stripe_customer_id,
        "product_id": settings.METRONOME_CREDITS_PRODUCT_ID,
        "credit_type_id": settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
        "description": f"isol8 top-up owner={owner_id} amount_cents={amount_cents}",
        # Metronome's native idempotency identity is the UUID supplied by the
        # caller. DDB scopes that UUID to this workspace before we call it.
        "idempotency_key": request_id,
    }
    intent = await get_or_create_top_up_intent(owner_id, request_id, binding)
    if intent["state"] == "completed":
        await set_active_top_up_status(
            owner_id,
            intent["metronome_customer_id"],
            intent["metronome_contract_id"],
            "requested",
            intent_event_id=intent["event_id"],
        )
        return
    if intent["state"] in {"paid", "failed"}:
        return
    attempted_at = intent.get("provider_attempted_at")
    if attempted_at is not None and time.time() - int(attempted_at) >= _METRONOME_RETRY_WINDOW_SECONDS:
        await set_active_top_up_status(
            owner_id,
            intent["metronome_customer_id"],
            intent["metronome_contract_id"],
            "action_required",
            intent_event_id=intent["event_id"],
        )
        raise TopUpIntentConflict("top-up requires billing reconciliation")
    await _verify_workspace_stripe_link(
        client,
        stripe_customer_id=intent["stripe_customer_id"],
        metronome_customer_id=intent["metronome_customer_id"],
        metronome_contract_id=intent["metronome_contract_id"],
    )
    attempted_at = await record_top_up_provider_attempt(intent["event_id"])
    if time.time() - attempted_at >= _METRONOME_RETRY_WINDOW_SECONDS:
        await set_active_top_up_status(
            owner_id,
            intent["metronome_customer_id"],
            intent["metronome_contract_id"],
            "action_required",
            intent_event_id=intent["event_id"],
        )
        raise TopUpIntentConflict("top-up requires billing reconciliation")
    await _create_gated_commit(
        client,
        customer_id=intent["metronome_customer_id"],
        contract_id=intent["metronome_contract_id"],
        amount_cents=intent["amount_cents"],
        description=intent["description"],
        idempotency_key=intent["idempotency_key"],
        effective_at=intent["effective_at"],
    )
    await complete_top_up_intent(intent["event_id"])
    await set_active_top_up_status(
        owner_id,
        intent["metronome_customer_id"],
        intent["metronome_contract_id"],
        "requested",
        intent_event_id=intent["event_id"],
    )


async def grant_prepaid_credits(
    owner_id: str,
    amount_cents: int,
    *,
    idempotency_key: str,
    client: MetronomeClient | None = None,
) -> None:
    """Grant audited promotional/admin credits without collecting payment.

    This intentionally ungated helper remains for controlled grants (for
    example launch referral rewards). Customer purchases must use
    :func:`request_top_up`, which creates a Stripe payment-gated commit.
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
    description = f"isol8 checkout top-up owner={owner_id} amount_cents={amount_cents}"

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
        "Granted promotional Metronome credits for owner %s: %d cents",
        owner_id,
        amount_cents,
    )


async def grant_prepaid_credits_and_refresh_gate(
    owner_id: str,
    amount_cents: int,
    *,
    idempotency_key: str,
) -> None:
    """THE two-ledger grant sequence: ``grant_prepaid_credits`` (Metronome,
    the source of record), then the restore-only fast-gate refresh so a
    blocked/revoked owner is re-armed the moment the credits land instead of
    waiting out the gate's staleness window. Used by audited promotional
    grant paths such as launch referral milestones.

    Grant errors PROPAGATE — each caller decides what a failed grant means
    (the webhook 500s into Stripe redelivery; the milestone path stamps
    ``pending_grant`` on ``FundingError``). The refresh is best-effort: it
    acts only on a confirmed-positive balance so a Metronome read lag can
    never re-revoke the owner whose money just landed, and a refresh failure
    only defers to the gate's own lazy refresh."""
    await grant_prepaid_credits(owner_id, amount_cents, idempotency_key=idempotency_key)
    try:
        # Lazy: core.services.metronome_gate sits a layer above core.billing.
        from core.services.metronome_gate import apply_fresh_balance, read_net_balance_microcents

        balance_uc = await read_net_balance_microcents(owner_id)
        if balance_uc is not None and balance_uc > 0:
            await apply_fresh_balance(owner_id, balance_uc)
    except Exception:  # noqa: BLE001 — the grant landed; the gate's lazy refresh catches up
        logger.warning("Post-grant balance refresh failed for owner %s", owner_id, exc_info=True)


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
