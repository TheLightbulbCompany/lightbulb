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
from typing import Any, Awaitable, Callable

import stripe
from fastapi import Response

from core.observability.metrics import put_metric, timing
from core.repositories import billing_repo
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
    # Customer-side fix, deliberately BEFORE owner resolution: the default
    # payment method matters to Metronome regardless of whether we can map
    # the subscription to a billing row.
    await _maybe_set_customer_default_payment_method(event_data)
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
        # provider_choice lives on billing_accounts (per-owner), not on
        # the user row. /trial-checkout writes synchronously before
        # creating the Stripe Checkout session; this webhook write is
        # the idempotent backup for any legacy Stripe sessions that
        # predate the synchronous write, or for direct-API callers who
        # skipped the /trial-checkout entry point.
        try:
            await billing_repo.set_provider_choice(
                account["owner_id"],
                provider_choice=metadata_provider,
                byo_provider=metadata_byo if metadata_provider == "byo_key" else None,
                # create_flat_fee_checkout threads the subscribing human's
                # Clerk id through subscription metadata; for chatgpt_oauth
                # that human is the workspace owner / initial connector.
                # None just leaves any synchronously-written connector
                # untouched.
                connector_user_id=(metadata.get("clerk_user_id") if metadata_provider == "chatgpt_oauth" else None),
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

    Trial-checkout sessions get their state from the subscription
    handlers above; only credit-top-up sessions (metadata
    ``purpose=credit_top_up``) do work here:

      1. Grant the paid-for credits as an UNGATED Metronome commit —
         the money is already collected by THIS checkout, so the grant
         must never generate a second Metronome→Stripe invoice
         (grant_prepaid_credits is access-schedule-only, live-verified
         to release instantly with zero invoices).
      2. Flip the billing row's ``credits_auto_charge`` flag when the
         customer opted in — the session that just succeeded carries the
         ``setup_future_usage=off_session`` consent, so success here is
         the only legitimate place the flag turns on.
      3. Run the restore-only gate refresh so a revoked owner is re-armed
         the moment the credits land (same hook as the one-click path).
    """
    session = event["data"]["object"]
    if (session.get("metadata") or {}).get("purpose") != "credit_top_up":
        return Response(status_code=200)

    # Only grant when Stripe has actually collected payment.
    #   - "paid"               : card cleared synchronously.
    #   - "no_payment_required": 100%-off coupon → $0 charge → still a
    #                            valid grant (we absorb it).
    #   - "unpaid"             : delayed method (ACH etc.) — Stripe fires
    #                            checkout.session.completed immediately,
    #                            then async_payment_succeeded (or _failed)
    #                            later. Wait for the async event so a
    #                            failed ACH never leaves free credits.
    payment_status = session.get("payment_status")
    if payment_status not in ("paid", "no_payment_required"):
        put_metric("credit.top_up.deferred", dimensions={"payment_status": payment_status or "unknown"})
        logger.info(
            "Credit top-up deferred for session %s — payment_status=%s",
            session.get("id"),
            payment_status,
        )
        return Response(status_code=200)

    metadata = session["metadata"]
    owner_id = metadata.get("owner_id")
    amount_cents_str = metadata.get("amount_cents")
    if not owner_id or not amount_cents_str:
        logger.error("Credit top-up checkout.session missing metadata: %s", session.get("id"))
        return Response(status_code=200)

    # Grant the REQUESTED amount, not what Stripe charged — a 100%-off
    # internal coupon charges nothing but still funds the full balance
    # (the coupon is us absorbing the cost).
    from core.billing.metronome_funding import grant_prepaid_credits

    # Session id as the Metronome Idempotency-Key: one completed Checkout
    # grants exactly once, however many times Stripe redelivers (or fires
    # completed + async_payment_succeeded back-to-back). A failure here
    # re-raises so the webhook 500s and Stripe redelivers — paid-for
    # credits must never be silently dropped.
    await grant_prepaid_credits(
        owner_id,
        int(amount_cents_str),
        idempotency_key=f"checkout-{session['id']}",
    )

    if metadata.get("auto_charge_opt_in") == "true":
        try:
            await billing_repo.set_credits_auto_charge(owner_id, enabled=True)
            put_metric("billing.auto_charge.enabled")
        except Exception:
            # The credits themselves landed; consent can be re-granted on a
            # later checkout. Don't fail the webhook into redelivery (the
            # grant above would replay harmlessly, but the noise is real).
            logger.exception("Failed to persist auto-charge opt-in for owner %s", owner_id)

    # Restore-only refresh (same contract as the one-click top-up path):
    # act only on a confirmed-positive balance so a Metronome read lag can
    # never re-revoke the owner who just paid.
    try:
        from core.services.metronome_gate import apply_fresh_balance, read_net_balance_microcents

        balance_uc = await read_net_balance_microcents(owner_id)
        if balance_uc is not None and balance_uc > 0:
            await apply_fresh_balance(owner_id, balance_uc)
    except Exception:
        logger.warning("Post-checkout balance refresh failed for owner %s", owner_id, exc_info=True)

    put_metric("credit.top_up.granted", dimensions={"source": "checkout"})
    return Response(status_code=200)


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
}
