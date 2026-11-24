"""Per-event handlers for the Stripe webhook dispatcher.

`routers/billing.py:handle_stripe_webhook` used to be a 230-line if/elif
chain dispatching on `event["type"]`. Each branch is independent — they
share only the owner-account resolution and the (already-extracted)
dedup check — so the chain factors cleanly into a
`{event_type: handler}` dispatch table plus this module of handler
functions.

Each handler returns either:
- `None`     → the router responds with the default `{"status": "ok"}`
- `Response` → the router returns that response verbatim

Handlers receive an `Event` envelope so they can read `event["data"]`
or `event["type"]` if they need to disambiguate.

Credit top-ups no longer pass through Stripe webhooks at all: funding is a
Metronome payment-gated commit (core/billing/metronome_funding.py) that
Metronome collects via the customer's billing-provider configuration, so
only the subscription lifecycle remains here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

import stripe
from fastapi import Response

from core.config import settings
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric, timing
from core.observability.product_analytics import capture_product_event
from core.repositories import billing_repo
from core.services.billing_service import BillingService

logger = logging.getLogger(__name__)


# Namespace for the deterministic ``trial_converted`` event uuid: Stripe delivers several events per
# subscription change, each can see ``old_status == "trial_local"``, and PostHog dedupes on the uuid.
_TRIAL_CONVERTED_NAMESPACE = uuid.UUID("2b7e3c1a-9d4f-4e6b-8a2c-5f1d7e9b3c60")


# ---------------------------------------------------------------------------
# Owner-account resolution (moved verbatim from routers/billing.py)
# ---------------------------------------------------------------------------


async def resolve_owner_account(subscription_event_data: dict) -> dict | None:
    """Resolve the billing-accounts row for a Stripe subscription webhook.

    Stripe customers are now keyed by email and can fan out to multiple
    DDB rows (one human → personal + org rows sharing one Stripe customer),
    so the GSI on ``stripe_customer_id`` is no longer 1:1.

    Resolution order:
      1. ``subscription.metadata.workspace_id`` — canonical workspace binding
         set by unified Checkout. ``owner_id`` remains a legacy fallback.
      2. ``stripe_customer_id`` GSI → if exactly one match, use it
         (legacy 1:1 case). If multiple, filter by
         ``stripe_subscription_id == event.id``.
    """
    metadata = subscription_event_data.get("metadata") or {}
    workspace_id = metadata.get("workspace_id")
    owner_id = workspace_id or metadata.get("owner_id")
    if owner_id:
        account = await billing_repo.get_by_owner_id(owner_id)
        if account and account.get("stripe_customer_id") == subscription_event_data.get("customer"):
            return account
        # workspace_id is an explicit canonical binding. A missing/mismatched
        # row must not fall through to a shared-customer guess.
        if workspace_id:
            put_metric("stripe.webhook.owner_resolve", dimensions={"path": "workspace_metadata_miss"})
            return None
        put_metric("stripe.webhook.owner_resolve", dimensions={"path": "metadata_miss"})

    customer_id = subscription_event_data.get("customer")
    if not customer_id:
        put_metric("stripe.webhook.owner_resolve", dimensions={"path": "unresolved"})
        return None

    candidates = await billing_repo.list_by_stripe_customer_id(customer_id)
    if not candidates:
        put_metric("stripe.webhook.owner_resolve", dimensions={"path": "unresolved"})
        return None
    if len(candidates) == 1:
        put_metric("stripe.webhook.owner_resolve", dimensions={"path": "customer_id_legacy"})
        return candidates[0]

    sub_id = subscription_event_data.get("id")
    matches = [c for c in candidates if c.get("stripe_subscription_id") == sub_id]
    if len(matches) == 1:
        put_metric("stripe.webhook.owner_resolve", dimensions={"path": "customer_id_subfilter"})
        return matches[0]

    # Ambiguous: shared customer with no subscription_id match (e.g. a
    # brand-new customer.subscription.created event from a row whose
    # checkout was fired before this change deployed and so lacks
    # metadata.owner_id). Surface as unresolved rather than guessing.
    logger.warning(
        "Ambiguous Stripe webhook owner resolution: customer=%s sub=%s candidates=%d",
        customer_id,
        sub_id,
        len(candidates),
    )
    put_metric("stripe.webhook.owner_resolve", dimensions={"path": "ambiguous"})
    return None


# ---------------------------------------------------------------------------
# Per-event handlers
# ---------------------------------------------------------------------------


async def _maybe_set_customer_default_payment_method(event_data: dict[str, Any]) -> None:
    """Promote the subscription's payment method to the CUSTOMER's invoice
    default when the customer has none.

    Stripe Checkout attaches the collected payment method to the
    SUBSCRIPTION only — the Customer's invoice_settings.default_payment_method
    stays empty. Metronome's charge_automatically collection charges the
    CUSTOMER default, so without this promotion every Metronome invoice
    (payment-gated credit top-ups, usage) fails for every
    checkout-onboarded customer.

    Idempotent (an existing customer default is never clobbered) and
    best-effort: a Stripe failure here must not fail the webhook — Stripe
    would retry the whole event, and the subscription-state sync is the
    part that matters. The metrics keep gaps operator-visible.
    """
    payment_method = event_data.get("default_payment_method")
    customer_id = event_data.get("customer")
    if not payment_method or not customer_id:
        return
    # Webhook payloads carry the payment method as an id string unless the
    # event was built with expansions; accept both shapes.
    if isinstance(payment_method, dict):
        payment_method = payment_method.get("id")
        if not payment_method:
            return
    try:
        with timing("stripe.api.latency", {"op": "customers.retrieve"}):
            customer = stripe.Customer.retrieve(customer_id)
        if (customer.get("invoice_settings") or {}).get("default_payment_method"):
            return
        with timing("stripe.api.latency", {"op": "customers.modify"}):
            stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method})
        put_metric("billing.default_pm.set")
        logger.info(
            "Set Stripe customer %s default payment method %s (from subscription %s)",
            customer_id,
            payment_method,
            event_data.get("id"),
        )
    except Exception:
        put_metric("billing.default_pm.error")
        logger.exception("Failed to set default payment method for Stripe customer %s", customer_id)


def _subscriber_distinct_id(account: dict, event_data: dict) -> str | None:
    """Clerk USER id to key PostHog events on, so server-side billing events
    merge onto the same person the frontend ``identify()``s. Personal owners:
    ``owner_id`` IS the Clerk user id. Org owners: ``owner_id`` is the org id,
    so prefer the subscribing human's ``clerk_user_id`` threaded through
    checkout metadata (the subscription checkout sets it)."""
    metadata = event_data.get("metadata") or {}
    return metadata.get("clerk_user_id") or account.get("owner_id")


async def handle_subscription_state(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`customer.subscription.created` + `customer.subscription.updated`.

    Only the voice plan's subscription is synced from here, and the customer's default payment method is set for
    Metronome. The Spark handler (`os_billing.stripe_handler`) runs before this one.
    """
    event_type = event["type"]
    event_data = event["data"]["object"]
    put_metric(
        "stripe.subscription",
        dimensions={
            "event": event_type.split(".")[-1],
            "status": event_data.get("status", "unknown"),
        },
    )
    # Customer-side fix, deliberately BEFORE owner resolution: the default
    # payment method matters to Metronome regardless of whether we can map
    # the subscription to a billing row.
    await _maybe_set_customer_default_payment_method(event_data)
    from core.billing import voice_plan

    if voice_plan.is_voice_subscription(event_data):  # the $250 voice plan is not a workspace subscription
        await voice_plan.sync_subscription(event_data)
        return None
    # Every other subscription was a workspace plan: the Workspace, Base and Ultra prices are gone, so nothing
    # else is synced here (the Spark handler runs before this one).
    return None


async def handle_trial_will_end(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`customer.subscription.trial_will_end`.

    Plan 3 §7.2 + §8.2: Stripe fires this 3 days before trial_end. We
    just emit a metric so we can see how often it fires; Stripe sends
    its own default reminder email regardless. A branded reminder
    email via SES is a follow-up.
    """
    event_data = event["data"]["object"]
    put_metric("trial.will_end_3day")
    account = await resolve_owner_account(event_data)
    if account:
        logger.info(
            "Trial will end in 3 days for owner %s (trial_end=%s)",
            account["owner_id"],
            event_data.get("trial_end"),
        )
        capture_product_event(
            distinct_id=_subscriber_distinct_id(account, event_data),
            event="trial_will_end",
            properties={"workspace_id": account.get("owner_id"), "trial_end": event_data.get("trial_end")},
        )
    return None


async def handle_subscription_deleted(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`customer.subscription.deleted`."""
    event_data = event["data"]["object"]
    put_metric("stripe.subscription", dimensions={"event": "deleted"})
    from core.billing import voice_plan

    if voice_plan.is_voice_subscription(event_data):
        await voice_plan.sync_subscription(event_data)
        return None
    account = await resolve_owner_account(event_data)
    if not account:
        return None
    # Customer-analytics bridge: record the churn on the person BEFORE teardown
    # (so it fires even if container/Paperclip teardown throws). ``was_trialing``
    # splits trial-abandon (activation problem) from post-charge churn (value
    # problem) — two different failures needing different fixes.
    capture_product_event(
        distinct_id=_subscriber_distinct_id(account, event_data),
        event="subscription_cancelled",
        properties={
            "workspace_id": account.get("owner_id"),
            "was_trialing": account.get("subscription_status") == "trialing",
            "provider_choice": account.get("provider_choice"),
            "cancel_at_period_end": bool(event_data.get("cancel_at_period_end")),
        },
        set_person={"subscription_status": "canceled", "is_subscribed": False},
    )
    await billing_service.cancel_subscription(account)
    logger.info("Subscription cancelled for owner %s", account["owner_id"])

    # Reset the pricing tier to the implicit standard plan — teardown removes the
    # container but leaves the billing row, and a lingering plan="ultra_pro"
    # would misreport the tier (and, if the owner re-subscribes onto a flat sub,
    # the first flat-price webhook would read the stale ultra_pro and treat it as
    # a downgrade). Guarded like the other set_plan calls: best-effort so a
    # transient DDB failure never 500s the webhook back to Stripe, and set_plan
    # is attribute_exists-guarded so an already-deleted row is a silent no-op.
    try:
        await billing_repo.set_plan(account["owner_id"], plan="standard", now_iso=utc_now_iso())
    except Exception:
        logger.exception("Failed to reset plan to standard on cancel for owner %s", account["owner_id"])

    # Owner-facts refresh + container teardown + Paperclip disable, but ONLY if
    # nothing else is still paying. Stripe ending is no longer the same question
    # as the entitlement ending: an owner can hold a live Apple subscription on
    # the same row, in which case the shared status stays active and tearing
    # their container down would take away exactly what they are still paying
    # for — with no further webhook coming to put it back.
    from core.services.entitlement_teardown import teardown_if_unentitled

    await teardown_if_unentitled(account["owner_id"], trigger="subscription_deleted")
    return None


async def handle_invoice_payment_failed(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    event_data = event["data"]["object"]
    put_metric("stripe.subscription", dimensions={"event": "payment_failed"})
    logger.warning("Payment failed for customer %s", event_data.get("customer"))
    return None


_TRIAL_CONVERTED_ONCE_TTL_S = 10 * 365 * 86400
_INVOICE_PAID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:invoice_paid")


def _invoice_subscription(invoice: dict[str, Any]) -> tuple[str | None, dict]:
    """``(subscription id, subscription metadata)`` across Stripe API versions: the newer
    ``parent.subscription_details`` and the older top-level ``subscription``."""
    details = ((invoice.get("parent") or {}).get("subscription_details")) or invoice.get("subscription_details") or {}
    sub = details.get("subscription") or invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return sub, details.get("metadata") or {}


def invoice_at_list(invoice: dict[str, Any]) -> bool:
    """Paid at list: money moved and no discount of any kind (09-analytics: coupons are not
    revenue, six workspaces sit on a 100% internal coupon)."""
    discounted = bool(invoice.get("discounts")) or any(
        int(d.get("amount") or 0) > 0 for d in invoice.get("total_discount_amounts") or []
    )
    return int(invoice.get("amount_paid") or 0) > 0 and not discounted


async def handle_invoice_paid(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`invoice.paid` (T23): ``invoice_paid {at_list}`` on every paid invoice, and
    ``trial_converted`` once per workspace, on its first invoice paid at list. Telemetry only;
    billing state stays with the subscription events. Never raises into the webhook."""
    invoice = event["data"]["object"]
    put_metric("stripe.invoice", dimensions={"event": "paid"})
    sub_id, sub_metadata = _invoice_subscription(invoice)
    account = await resolve_owner_account({"metadata": sub_metadata, "customer": invoice.get("customer"), "id": sub_id})
    if not account:
        return None
    owner_id = account["owner_id"]
    at_list = invoice_at_list(invoice)
    props = {
        "workspace_id": owner_id,
        "invoice_id": invoice.get("id"),
        "amount_paid": invoice.get("amount_paid"),
        "currency": invoice.get("currency"),
        "plan": account.get("plan"),
        "billing_reason": invoice.get("billing_reason"),
        "at_list": at_list,
    }
    distinct_id = _subscriber_distinct_id(account, {"metadata": sub_metadata})
    capture_product_event(
        distinct_id=distinct_id,
        event="invoice_paid",
        properties=props,
        event_uuid=str(uuid.uuid5(_INVOICE_PAID_NAMESPACE, str(invoice.get("id")))),
    )
    if at_list and sub_id:  # a subscription's invoice: a credit top-up is not a conversion
        from core.services import webhook_dedup

        try:
            first = await webhook_dedup.record_event_or_skip(
                f"trial-converted:{owner_id}", source="analytics", ttl_seconds=_TRIAL_CONVERTED_ONCE_TTL_S
            )
        except Exception:
            logger.warning("trial_converted marker failed for %s", owner_id, exc_info=True)
            return None
        if first is webhook_dedup.WebhookDedupResult.RECORDED:
            capture_product_event(
                distinct_id=distinct_id,
                event="trial_converted",
                properties={"workspace_id": owner_id, "plan": account.get("plan"), "invoice_id": invoice.get("id")},
                event_uuid=str(uuid.uuid5(_TRIAL_CONVERTED_NAMESPACE, owner_id)),
            )
    return None


async def handle_voice_plan_reversal(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`charge.refunded` (full refunds only) and `charge.dispute.created`: a voice-plan payment taken
    back ends the plan and raises a Needs-you card. Charges of anything else are ignored."""
    from core.billing import voice_plan
    from core.dynamodb import run_in_thread

    obj = event["data"]["object"]
    if event["type"] == "charge.refunded":
        if not obj.get("refunded"):  # partial refund: the plan stands
            return None
        charge_id = obj["id"]
    elif str(obj.get("status", "")).startswith("warning_"):  # an inquiry, not yet a chargeback
        return None
    else:
        charge_id = obj.get("charge")
    if not charge_id or not settings.STRIPE_VOICE_PLAN_PRICE_ID:
        return None
    key = settings.STRIPE_SECRET_KEY
    charge = await run_in_thread(stripe.Charge.retrieve, charge_id, api_key=key)
    invoice = charge.get("invoice")
    if not invoice and charge.get("payment_intent"):  # newer API versions dropped Charge.invoice
        payments = await run_in_thread(
            stripe.InvoicePayment.list,
            payment={"type": "payment_intent", "payment_intent": charge["payment_intent"]},
            api_key=key,
        )
        invoice = next((p.get("invoice") for p in payments.get("data") or []), None)
    if isinstance(invoice, str):
        invoice = await run_in_thread(stripe.Invoice.retrieve, invoice, api_key=key)
    subscription_id, _ = _invoice_subscription(invoice or {})
    if not subscription_id:
        return None
    owner_id = await voice_plan.end_for_reversal(subscription_id)
    if owner_id:
        put_metric("stripe.voice_plan", dimensions={"event": "reversed"})
        try:
            await voice_plan.announce_reversal(owner_id)
        except Exception:  # noqa: BLE001  the plan already ended; a missing card costs visibility only
            logger.warning("voice plan reversal card failed owner=%s", owner_id, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict[str, Any], BillingService], Awaitable[Response | None]]

STRIPE_WEBHOOK_HANDLERS: dict[str, HandlerFn] = {
    "customer.subscription.created": handle_subscription_state,
    "customer.subscription.updated": handle_subscription_state,
    "customer.subscription.trial_will_end": handle_trial_will_end,
    "customer.subscription.deleted": handle_subscription_deleted,
    "invoice.payment_failed": handle_invoice_payment_failed,
    "invoice.payment_action_required": handle_invoice_payment_failed,
    "invoice.paid": handle_invoice_paid,
    "charge.refunded": handle_voice_plan_reversal,
    "charge.dispute.created": handle_voice_plan_reversal,
}
