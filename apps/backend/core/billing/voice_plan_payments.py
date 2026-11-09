"""Voice collection truth: exact Metronome invoice/contract binding and canonical Stripe payment state."""

from datetime import UTC, datetime
import hashlib
import json

import httpx
import stripe

from core.billing import metronome_client
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import billing_repo


def utc(raw: str) -> str:
    return datetime.fromisoformat(raw).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def refresh(owner_id: str) -> None:
    """Repairable side-effect receipt, separate from payment truth; facts still have their existing sole store."""
    from core.billing.voice_billing import has_voice_plan
    from core.services import openrouter_keys, owner_facts, proactive
    from core.services.provision_dispatch import dispatch_reconcile

    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if not account.get("metronome_voice_contract_id"):
        return
    revision = hashlib.sha256(
        json.dumps(
            {
                "active": has_voice_plan(account),
                "periods": account.get("voice_plan_paid_periods") or {},
                "debts": sorted(account.get("voice_plan_unpaid_invoice_ids") or []),
                "ends": account.get("voice_plan_ending_at"),
                "cancel_target": account.get("voice_plan_cancel_target"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if account.get("voice_plan_runtime_sync_revision") == revision:
        return
    if settings.OPENROUTER_WORKSPACE_ID:
        await openrouter_keys.relimit_owner_key(owner_id)
    await owner_facts.write_owner_facts(owner_id)
    if has_voice_plan(account):
        if not await dispatch_reconcile(owner_id):
            raise RuntimeError("voice runtime reconcile could not start; retry")
    else:
        await proactive.ensure(owner_id)
    await billing_repo.set_voice_runtime_sync_revision(
        owner_id, contract_id=account["metronome_voice_contract_id"], revision=revision
    )


async def default_card(customer_id: str) -> bool:
    """Metronome charge_automatically needs this customer's actual invoice-default card."""
    if not settings.STRIPE_SECRET_KEY:
        return False
    customer = await run_in_thread(stripe.Customer.retrieve, customer_id, api_key=settings.STRIPE_SECRET_KEY)
    if customer.get("deleted") or customer.get("id") != customer_id:
        return False
    method = (customer.get("invoice_settings") or {}).get("default_payment_method")
    if method:
        method_id = method.get("id") if isinstance(method, dict) else method
        pm = await run_in_thread(stripe.PaymentMethod.retrieve, method_id, api_key=settings.STRIPE_SECRET_KEY)
        return pm.get("customer") == customer_id and pm.get("type") == "card"
    source = customer.get("default_source")
    if not source:
        return False
    source_id = source.get("id") if isinstance(source, dict) else source
    card = await run_in_thread(
        stripe.Customer.retrieve_source, customer_id, source_id, api_key=settings.STRIPE_SECRET_KEY
    )
    return card.get("object") == "card" and card.get("customer") == customer_id


async def _apply(account: dict, invoice: dict, stripe_invoice: dict) -> bool:
    external = invoice.get("external_invoice") or {}
    if (
        invoice.get("contract_id") == account.get("metronome_voice_contract_id")
        and invoice.get("customer_id") == account.get("metronome_customer_id")
        and (stripe_invoice.get("metadata") or {}).get("metronome_id") == invoice.get("id")
        and not external.get("invoice_id")
    ):
        raise RuntimeError("voice invoice export binding not visible yet; retry")
    if not (
        invoice.get("contract_id") == account.get("metronome_voice_contract_id")
        and invoice.get("customer_id") == account.get("metronome_customer_id")
        and stripe_invoice.get("customer") == account.get("stripe_customer_id")
        and external.get("billing_provider_type") == "stripe"
        and external.get("invoice_id") == stripe_invoice.get("id")
        and (stripe_invoice.get("metadata") or {}).get("metronome_id") == invoice.get("id")
    ):
        return False
    lines = [
        line
        for line in invoice.get("line_items") or []
        if line.get("product_id") == settings.METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID
        and line.get("type") == "subscription"
        and line.get("quantity") == 1
    ]
    period_start = max((utc(line["starting_at"]) for line in lines), default=utc(invoice["start_timestamp"]))
    paid_through = max((utc(line["ending_before"]) for line in lines), default=None)
    if (
        stripe_invoice.get("status") == "paid"
        and stripe_invoice.get("paid") is not False
        and stripe_invoice.get("currency") == "usd"
        and int(stripe_invoice.get("amount_paid") or 0) >= int(invoice.get("total") or 0)
    ):
        state = "paid"
    elif stripe_invoice.get("status") == "paid":
        return False  # a nonzero Metronome invoice cannot grant access from a zero/incorrect Stripe export
    elif stripe_invoice.get("status") == "void":
        state = "void"
    elif stripe_invoice.get("status") in {"open", "uncollectible"} and (
        stripe_invoice.get("attempted") or stripe_invoice.get("status") == "uncollectible"
    ):
        state = "failed"
    else:
        state = "pending"  # show the owned hosted payment link while waiting; grant no paid coverage
    return await billing_repo.record_voice_payment(
        account["owner_id"],
        contract_id=account["metronome_voice_contract_id"],
        invoice_id=stripe_invoice["id"],
        period_start=period_start,
        paid_through=paid_through,
        state=state,
        action_url=stripe_invoice.get("hosted_invoice_url") if state in {"failed", "pending"} else None,
    )


async def apply_provider_invoice(account: dict, invoice: dict) -> bool:
    stripe_id = (invoice.get("external_invoice") or {}).get("invoice_id")
    if not stripe_id or invoice.get("contract_id") != account.get("metronome_voice_contract_id"):
        return False
    current = await run_in_thread(stripe.Invoice.retrieve, stripe_id, api_key=settings.STRIPE_SECRET_KEY)
    return await _apply(account, invoice, current)


async def reconcile(owner_id: str, *, client=None) -> dict:
    """Repair an invoice webhook that arrived before the contract id was persisted."""
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if not account.get("metronome_voice_contract_id"):
        return account
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10) as http:
            return await reconcile(owner_id, client=metronome_client.for_settings(http))
    summaries = await client.list_customer_invoices(customer_id=account["metronome_customer_id"], status=None)
    for summary in summaries:
        if summary.get("contract_id") and summary["contract_id"] != account["metronome_voice_contract_id"]:
            continue
        invoice = await client.get_customer_invoice(
            customer_id=account["metronome_customer_id"], invoice_id=summary["id"]
        )
        await apply_provider_invoice(account, invoice)
    await refresh(owner_id)
    return await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or account


async def on_invoice(event: dict, *, client=None) -> bool:
    """Metadata locates an invoice; provider reads prove workspace, contract and payment ownership."""
    snapshot = event["data"]["object"]
    invoice_id = (snapshot.get("metadata") or {}).get("metronome_id")
    customer_id = snapshot.get("customer")
    if not invoice_id or not customer_id:
        return False
    candidates = await billing_repo.list_by_stripe_customer_id(customer_id)
    if not candidates:
        return False
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10) as http:
            return await on_invoice(event, client=metronome_client.for_settings(http))
    for candidate in candidates:
        account = await billing_repo.get_by_owner_id(candidate["owner_id"], consistent_read=True) or {}
        if account.get("stripe_customer_id") != customer_id or not account.get("metronome_voice_contract_id"):
            continue
        try:
            invoice = await client.get_customer_invoice(
                customer_id=account["metronome_customer_id"], invoice_id=invoice_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue  # another Metronome customer sharing the Stripe customer
            raise
        if invoice.get("contract_id") != account["metronome_voice_contract_id"]:
            continue  # credits and other contracts never change the voice entitlement
        current = await run_in_thread(stripe.Invoice.retrieve, snapshot["id"], api_key=settings.STRIPE_SECRET_KEY)
        applied = await _apply(account, invoice, current)
        await refresh(account["owner_id"])
        return applied
    return False
