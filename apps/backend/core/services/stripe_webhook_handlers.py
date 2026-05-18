"""Per-event handlers for the Stripe webhook dispatcher.

`routers/billing.py:handle_stripe_webhook` used to be a 230-line if/elif
chain dispatching on `event["type"]`. Each branch is independent — they
share only the owner-account resolution and the (already-extracted)
dedup check — so the chain factors cleanly into a
`{event_type: handler}` dispatch table plus this module of handler
functions.

Each handler returns either:
- `None`     → the router responds with the default `{"status": "ok"}`
- `Response` → the router returns that response verbatim (used by
                credit-top-up validation guards that need to early-exit
                with `200` before the credit grant)

Handlers receive an `Event` envelope so they can read `event["data"]`
or `event["type"]` if they need to disambiguate (the credit handler
emits per-event-type metrics).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import Response

from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import credit_ledger
from core.services.billing_service import BillingService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Owner-account resolution (moved verbatim from routers/billing.py)
# ---------------------------------------------------------------------------


async def resolve_owner_account(subscription_event_data: dict) -> dict | None:
    """Resolve the billing-accounts row for a Stripe subscription webhook.

    Stripe customers are now keyed by email and can fan out to multiple
    DDB rows (one human → personal + org rows sharing one Stripe customer),
    so the GSI on ``stripe_customer_id`` is no longer 1:1.

    Resolution order:
      1. ``subscription.metadata.owner_id`` — set at checkout time, pins
         each subscription to a single billing row. Always prefer this.
      2. ``stripe_customer_id`` GSI → if exactly one match, use it
         (legacy 1:1 case). If multiple, filter by
         ``stripe_subscription_id == event.id``.
    """
    metadata = subscription_event_data.get("metadata") or {}
    owner_id = metadata.get("owner_id")
    if owner_id:
        account = await billing_repo.get_by_owner_id(owner_id)
        if account:
            return account
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


async def handle_subscription_state(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`customer.subscription.created` + `customer.subscription.updated`.

    Single source of truth for subscription-state sync. Fires on trial
    start (.created), trial→active transitions, payment-method updates,
    past_due transitions, and cancellation-at-period-end (all .updated).
    We just persist the Stripe-side state so the frontend can render
    trial banners + access gates without a Stripe round-trip.

    CRITICAL: Stripe Checkout for a brand-new trial emits
    customer.subscription.CREATED first; .updated only fires on later
    state transitions. Without the .created branch, the user's first
    trial never persists subscription_status or provider_choice — they
    get stuck on the ProviderPicker after returning from Checkout
    because is_subscribed stays False.
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
    account = await resolve_owner_account(event_data)
    if not account:
        return None
    await billing_repo.set_subscription(
        owner_id=account["owner_id"],
        subscription_id=event_data["id"],
        status=event_data.get("status", "active"),
        trial_end=event_data.get("trial_end"),
    )
    metadata = event_data.get("metadata") or {}
    metadata_provider = metadata.get("provider_choice")
    metadata_byo = metadata.get("byo_provider")
    if metadata_provider in ("chatgpt_oauth", "byo_key", "bedrock_claude"):
        # Workstream B: provider_choice now lives on billing_accounts
        # (per-owner), not on the user row. /trial-checkout writes
        # synchronously before creating the Stripe Checkout session;
        # this webhook write is the idempotent backup for any legacy
        # Stripe sessions that predate the synchronous write, or for
        # direct-API callers who skipped the /trial-checkout entry
        # point.
        try:
            await billing_repo.set_provider_choice(
                account["owner_id"],
                provider_choice=metadata_provider,
                byo_provider=metadata_byo if metadata_provider == "byo_key" else None,
                owner_type=account.get("owner_type", "personal"),
            )
        except ValueError:
            # Org invariant violation (chatgpt_oauth on org). Should
            # not happen post-Workstream-B because /trial-checkout's
            # router-level guard rejects the same combo, but
            # defense-in-depth.
            logger.exception(
                "Webhook tried to set invalid provider_choice for owner %s",
                account["owner_id"],
            )
        except Exception:
            logger.exception(
                "Failed to persist provider_choice for owner %s",
                account["owner_id"],
            )
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
    return None


async def handle_subscription_deleted(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`customer.subscription.deleted`."""
    event_data = event["data"]["object"]
    put_metric("stripe.subscription", dimensions={"event": "deleted"})

    account = await resolve_owner_account(event_data)
    if not account:
        return None
    await billing_service.cancel_subscription(account)
    logger.info("Subscription cancelled for owner %s", account["owner_id"])

    # Tear down the user's container — they no longer have an active
    # subscription. Best-effort; log and continue on failure so we
    # don't 500 the webhook back to Stripe.
    try:
        from core.containers import get_ecs_manager

        await get_ecs_manager().delete_user_service(account["owner_id"])
    except Exception:
        logger.exception(
            "Container teardown on subscription.deleted failed for owner %s",
            account["owner_id"],
        )

    # Disable Paperclip for this owner with a 30-day grace window —
    # T13's cleanup cron purges the Paperclip-side artifacts after
    # the grace elapses. Only THIS user is disabled; if they own a
    # multi-member org the other members keep access until their
    # own subscriptions cancel (or the org is deleted in Clerk).
    from core.services.paperclip_provisioning import (
        build_paperclip_provisioning,
        close_paperclip_provisioning,
    )

    provisioning = None
    try:
        provisioning = await build_paperclip_provisioning()
        await provisioning.disable(user_id=account["owner_id"])
        put_metric(
            "paperclip.webhook.disable",
            dimensions={"trigger": "subscription_deleted"},
        )
    except Exception:
        logger.exception(
            "Paperclip disable on subscription.deleted failed for owner %s",
            account["owner_id"],
        )
    finally:
        if provisioning is not None:
            await close_paperclip_provisioning(provisioning)
    return None


async def handle_invoice_payment_failed(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    event_data = event["data"]["object"]
    put_metric("stripe.subscription", dimensions={"event": "payment_failed"})
    logger.warning("Payment failed for customer %s", event_data.get("customer"))
    return None


async def handle_invoice_paid(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    event_data = event["data"]["object"]
    logger.info("Payment succeeded for customer %s", event_data.get("customer"))
    return None


async def handle_checkout_session(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """`checkout.session.completed` + `checkout.session.async_payment_succeeded`.

    We piggyback on the same webhook for two distinct flows; only
    credit-top-up sessions need ledger work here. Trial-checkout
    sessions get their state from customer.subscription.created
    / .updated above.
    """
    event_type = event["type"]
    session = event["data"]["object"]
    if session.get("metadata", {}).get("purpose") != "credit_top_up":
        return Response(status_code=200)

    # Only grant credits when Stripe has actually collected payment.
    #   - "paid"               : card payment cleared synchronously.
    #   - "no_payment_required": 100%-off coupon → $0 charge → still
    #                            a valid credit grant (we absorb the
    #                            cost via the coupon).
    #   - "unpaid"             : delayed-payment method (ACH /
    #                            bank transfer / Klarna) hasn't
    #                            settled yet. Stripe fires
    #                            checkout.session.completed
    #                            immediately for these, then later
    #                            fires async_payment_succeeded
    #                            (or async_payment_failed). We
    #                            wait for the async event before
    #                            crediting so a failed ACH doesn't
    #                            leave the user with free credits.
    # Codex P1 on PR #488.
    payment_status = session.get("payment_status")
    if payment_status not in ("paid", "no_payment_required"):
        put_metric(
            "credit.top_up.deferred",
            dimensions={"payment_status": payment_status or "unknown"},
        )
        logger.info(
            "Credit top-up deferred for session %s — payment_status=%s",
            session.get("id"),
            payment_status,
        )
        return Response(status_code=200)

    # owner_id is the credit ledger key — org_id for org context,
    # user_id for personal. See create_credit_top_up_checkout for
    # where we write it.
    metadata = session["metadata"]
    owner_id = metadata.get("owner_id")
    amount_cents_str = metadata.get("amount_cents")
    if not owner_id or not amount_cents_str:
        logger.error("Credit top-up checkout.session missing metadata: %s", session.get("id"))
        return Response(status_code=200)

    # Credit the *requested* amount, not what Stripe actually charged.
    # If the user redeemed a 100%-off internal coupon, Stripe charged
    # nothing but we still grant the full credit balance — the coupon
    # is effectively us absorbing the cost so internal teams can fund
    # their own usage without per-transaction Stripe fees. For partial
    # discounts the same rule applies: the user paid less, but the
    # ledger reflects the credits-promised side of the transaction.
    amount_cents = int(amount_cents_str)
    amount_microcents = amount_cents * 10_000
    await credit_ledger.top_up(
        owner_id,
        amount_microcents=amount_microcents,
        # Use session id as the idempotency key on the ledger side —
        # one Checkout completion = one credit grant, even if Stripe
        # retries the webhook delivery (or fires both completed +
        # async_payment_succeeded back-to-back).
        stripe_payment_intent_id=session.get("id"),
    )
    amount_total = session.get("amount_total")
    put_metric(
        "credit.top_up",
        value=(amount_total or 0) / 100.0,
        unit="None",
        dimensions={
            "source": "stripe_checkout",
            "discounted": "true" if amount_total != amount_cents else "false",
            "event": event_type.split(".")[-1],
        },
    )
    return None


async def handle_checkout_session_async_failed(
    event: dict[str, Any], billing_service: BillingService
) -> Response | None:
    """`checkout.session.async_payment_failed`.

    Delayed-payment method (ACH / bank transfer) was rejected after
    checkout.session.completed had already fired with payment_status
    = "unpaid". Because we *gate* the credit grant on payment_status,
    nothing was ever credited — but emit a metric so we can see the
    fail rate. Codex P1 on PR #488.
    """
    session = event["data"]["object"]
    if session.get("metadata", {}).get("purpose") == "credit_top_up":
        put_metric(
            "credit.top_up.payment_failed",
            dimensions={"source": "stripe_checkout"},
        )
        logger.warning(
            "Credit top-up payment failed for session %s (user=%s)",
            session.get("id"),
            session.get("metadata", {}).get("user_id"),
        )
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
    "invoice.paid": handle_invoice_paid,
    "checkout.session.completed": handle_checkout_session,
    "checkout.session.async_payment_succeeded": handle_checkout_session,
    "checkout.session.async_payment_failed": handle_checkout_session_async_failed,
}
