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

from fastapi import Response

from core.observability.metrics import put_metric
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
}
