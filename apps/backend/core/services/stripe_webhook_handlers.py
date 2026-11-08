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
from core.services.billing_service import BillingService, _is_front_desk_item

logger = logging.getLogger(__name__)


# A Checkout Session Stripe has actually SETTLED — the precondition for
# stamping the launch winner's coupon as consumed.
#   - "paid"               : card cleared synchronously.
#   - "no_payment_required": $0 due — a 100%-off coupon, which is exactly the
#                            shape of the launch winner's free Workspace month —
#                            still a completed purchase.
# Anything else ("unpaid") is a delayed method (ACH etc.): Stripe fires
# checkout.session.completed immediately and async_payment_succeeded (or
# _failed) later, so nothing may be granted or stamped until that second
# event arrives on this same handler.
_SETTLED_PAYMENT_STATUSES = ("paid", "no_payment_required")


# Namespace for the deterministic ``trial_converted`` event uuid: Stripe delivers several events per
# subscription change, each can see ``old_status == "trial_local"``, and PostHog dedupes on the uuid.
_TRIAL_CONVERTED_NAMESPACE = uuid.UUID("2b7e3c1a-9d4f-4e6b-8a2c-5f1d7e9b3c60")


async def on_entering_live(owner_id: str) -> None:
    """Side effects of a workspace's FIRST move onto a paid subscription, from either
    provider (Stripe here, Apple in apple_iap). Best-effort/deduped."""
    from core.services import launch_referral

    await launch_referral.on_subscription_activated(owner_id)


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


def _bridge_subscription_state(
    account: dict,
    event_data: dict,
    old_status: str | None,
    new_status: str,
    metadata_provider: str | None,
) -> None:
    """Land the subscription-state transition on the PostHog person (customer
    analytics) and keep billing state on the profile for segmentation.
    Complements — does not replace — the EMF ``stripe.subscription`` metric."""
    if new_status == old_status:
        return  # payment-method / no-op .updated — nothing to record
    provider = metadata_provider or account.get("provider_choice")
    set_person = {
        "subscription_status": new_status,
        "is_subscribed": new_status in ("trialing", "active"),
        "provider_choice": provider,
        "trial_end": event_data.get("trial_end"),
    }
    if new_status == "trialing" and old_status != "trialing":
        event = "trial_started"
    elif new_status == "active":
        # trial_converted is NOT a status change: it is the first paid, undiscounted invoice
        # (handle_invoice_paid, T23). A coupon or a $0 invoice turns a status active too.
        event = "subscription_activated"
    elif new_status == "past_due":
        event = "subscription_past_due"
    else:
        event = "subscription_status_changed"
    capture_product_event(
        distinct_id=_subscriber_distinct_id(account, event_data),
        event=event,
        properties={
            "workspace_id": account.get("owner_id"),
            "provider_choice": provider,
            "from_status": old_status,
            "to_status": new_status,
        },
        set_person=set_person,
    )


def _workspace_plan_from_subscription_items(event_data: dict[str, Any]) -> str | None:
    """Return the configured Workspace/Ultra plan for one recurring item only.

    A workspace subscription has exactly one recurring Stripe item.  Never
    persist a subscription event whose item shape cannot prove that invariant:
    accepting an unknown or multi-item payload can mask an unsupported
    recurring charge.
    """
    seat_price = settings.STRIPE_SEAT_PRICE_ID
    items = [
        item
        for item in (event_data.get("items") or {}).get("data") or []
        # The per-member seat item (T04) and the Front desk add-on are the allowed extra items.
        if not (seat_price and ((item or {}).get("price") or {}).get("id") == seat_price)
        and not _is_front_desk_item(item or {})
    ]
    if len(items) != 1:
        reason = "missing_or_multiple_items"
    else:
        price = (items[0] or {}).get("price") or {}
        price_id = price.get("id")
        if not isinstance(price.get("recurring"), dict):
            reason = "non_recurring_item"
        elif price_id == settings.STRIPE_BASE_PRICE_ID and price_id:
            return "base"
        elif price_id == settings.STRIPE_WORKSPACE_PRICE_ID and price_id:
            return "standard"
        elif price_id == settings.STRIPE_ULTRA_PRICE_ID and price_id:
            return "ultra_pro"
        else:
            reason = "unknown_price"
    put_metric("stripe.subscription.invalid_shape", dimensions={"reason": reason})
    logger.warning(
        "Ignoring subscription %s: expected one configured recurring Workspace or Ultra item (%s)",
        event_data.get("id"),
        reason,
    )
    return None


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
    # Capture the prior status BEFORE persisting the new one, so the PostHog
    # bridge below can detect transitions (esp. trialing -> active, the
    # trial->paid moment).
    old_status = account.get("subscription_status")
    # The id the row held BEFORE this event: the handler overwrites it below.
    stored_sub = account.get("stripe_subscription_id")
    new_status = event_data.get("status", "active")
    if (new_status in ("incomplete", "incomplete_expired") and old_status in ("trial_local", "active", "trialing")) or (
        # A subscription never re-enters its trial, and Checkout only opens a trial for a
        # trial_local row: a trialing event over a post-trial status is a stale, reordered delivery.
        new_status == "trialing" and old_status in ("active", "past_due", "unpaid", "canceled")
    ):
        # Checkout bills immediately (no Stripe trial), so a Checkout-born
        # subscription can surface here as `incomplete` (.created fires before
        # the first invoice settles) or `incomplete_expired` (checkout
        # abandoned). Neither may ever overwrite an access-granting status:
        #
        #   - over "trial_local": the write would (a) make the follow-on
        #     active event see old_status != "trial_local" and silently skip
        #     the trial→paid conversion reprovision below, leaving a paying
        #     customer on the locked-down trial box, and (b) strip a
        #     still-valid free-tier owner's platform access (`incomplete*` is
        #     not in _ACTIVE_STATUSES).
        #   - over "active"/"trialing": webhook delivery is UNORDERED
        #     (https://docs.stripe.com/webhooks#event-ordering) — the stale
        #     .created(incomplete) can land after .updated(active) already
        #     persisted, and writing it would block a customer who paid.
        #     Stripe's state machine never re-enters incomplete from a live
        #     state, so an incomplete event against a live row is always
        #     stale or out of order.
        #
        # Skip ALL writes: for a fresh checkout the active event carries the
        # same metadata and performs them; for a stale event the row is
        # already correct; an abandoned checkout leaves the free tier
        # untouched.
        logger.info(
            "Skipping stale %s persist for owner %s (stored status %s)",
            new_status,
            account["owner_id"],
            old_status,
        )
        return None
    new_plan = _workspace_plan_from_subscription_items(event_data)
    if new_plan is None:
        return None
    from core.services.apple_iap import apple_is_live, stripe_is_live

    if new_status in ("active", "trialing") and not stripe_is_live(account) and apple_is_live(account):
        # Two live subscriptions for one workspace: the purchase-intent/link fences
        # lost a race. Never block the Stripe write (the customer paid it); page
        # support so one side is refunded.
        put_metric("billing.dual_provider_live")
        logger.error("billing.dual_provider_live owner_id=%s subscription=%s", account["owner_id"], event_data["id"])
    await billing_repo.set_subscription(
        owner_id=account["owner_id"],
        subscription_id=event_data["id"],
        status=new_status,
        trial_end=event_data.get("trial_end"),
    )
    metadata = event_data.get("metadata") or {}
    metadata_provider = metadata.get("provider_choice")
    metadata_byo = metadata.get("byo_provider")
    provider_persisted = False
    # The provider backup below runs only on the transition INTO a live paid
    # status — the first usable event of a new subscription. That is usually
    # .created(active|trialing), but for a checkout-born sub whose .created
    # surfaced as `incomplete` (skipped entirely by the guard above), it is
    # the follow-up .updated(active) that performs the deferred writes.
    #
    # Events on an already-established subscription (renewals,
    # payment-method changes, and past_due/unpaid/paused recoveries) must
    # NEVER persist it: subscription metadata is a snapshot of the provider
    # chosen at checkout and is never updated by change_provider, so on those
    # events it can be stale history. Persisting it there clobbers a
    # post-onboarding provider switch back to the checkout-time value — and
    # set_provider_choice's REMOVE strips oauth_connector_user_id with it.
    #
    # Prior states are an ALLOWLIST of "no live subscription behind this row"
    # (fresh signup, local free tier, unsettled/dead checkout, terminal
    # cancel — a canceled owner can only come back via a new checkout with
    # fresh metadata). Anything else, including Stripe statuses that don't
    # exist yet, defaults to "existing subscription — skip". A None status
    # counts only when the row also carries no subscription id: with one, a
    # None-status row is a LEGACY PAID subscription (is_subscription_active
    # treats it as live, so it can have switched providers already).
    pre_live_statuses = ("trial_local", "incomplete", "incomplete_expired", "canceled")
    entering_live = new_status in ("active", "trialing") and (
        old_status in pre_live_statuses or (old_status is None and not account.get("stripe_subscription_id"))
    )
    if entering_live and metadata_provider in ("chatgpt_oauth", "byo_key"):
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
                # Never record the connector here. Subscription metadata is
                # unauthenticated snapshot data, and a stale or bogus
                # ``clerk_user_id`` recorded onto the row wedges the workspace
                # permanently: the provision gate waits on a ghost's tokens
                # while /start and the provider switch 403 every real human
                # (issue #2864 follow-up). The connector is recorded only by
                # authenticated writers — the synchronous trial-checkout
                # write, change-provider, and the poll-time set-once claim —
                # so an unset row stays claimable by the first real admin's
                # device flow instead of stranding on a phantom.
                connector_user_id=None,
            )
            provider_persisted = True
        except Exception:
            logger.exception(
                "Failed to persist provider_choice for owner %s",
                account["owner_id"],
            )

    # First-PAID side effects (referral credit, company-Stripe workflow): a card-on-file trial
    # (trial_local -> trialing, T05) has paid nothing, so they wait for its trialing -> active.
    # Any arrival at "active" from a non-active status counts (trial charge, or its recovery from
    # past_due); the hook is deduped inside, so a repeat for an old customer is a no-op.
    if new_status == "active" and (entering_live or old_status != "active"):
        # The Metronome customer is NOT created here: this transition dispatches a durable
        # reconcile (below), and ``reconcile_runtime`` ensures the customer for a ledger-eligible
        # non-ChatGPT/BYO owner -- an SFN execution a backend deploy cannot kill, where the old
        # fire-and-forget task here could die and Stripe's redelivery never re-enters this edge.
        await on_entering_live(account["owner_id"])

    plan_persisted = False
    try:
        await billing_repo.set_plan(account["owner_id"], plan=new_plan, now_iso=utc_now_iso())
        plan_persisted = True
    except Exception:
        logger.exception("Failed to persist plan for owner %s", account["owner_id"])
    if settings.OPENROUTER_WORKSPACE_ID:
        # A plan/status change can move the owner's OpenRouter key between the
        # trial and Base daily limits (the capped model's valve). Best-effort:
        # a key with no change is a no-op, and a failure never 500s Stripe.
        try:
            from core.services.openrouter_keys import relimit_owner_key

            await relimit_owner_key(account["owner_id"])
        except Exception:
            logger.exception("Failed to relimit OpenRouter key for owner %s", account["owner_id"])
    # Decided here, dispatched LAST (below): a failed dispatch raises so Stripe redelivers, and
    # everything before it must already have run once.
    reconcile_needed = (
        new_status in ("active", "trialing")
        or provider_persisted
        or (plan_persisted and account.get("plan") != new_plan)
    )
    if old_status == "trial_local" and new_status == "active":
        # The free trial checked out straight into paid billing (a card-on-file trial,
        # trial_local -> trialing (T05), has paid nothing and is not this). The conversion
        # itself is trial_converted on the first invoice paid at list (handle_invoice_paid,
        # T23). Several events per change can see old_status == "trial_local"; the
        # deterministic uuid makes PostHog keep one.
        capture_product_event(
            distinct_id=_subscriber_distinct_id(account, event_data),
            # The card went on at checkout; the paid moment is trial_converted on invoice.paid.
            event="trial_checkout_completed",
            properties={
                "workspace_id": account["owner_id"],
                "from_status": old_status,
                "to_status": new_status,
                # Same persisted-only rule as the bridge below: claim the metadata provider
                # only when its row write committed.
                "provider_choice": (metadata_provider if provider_persisted else None)
                or account.get("provider_choice"),
            },
            event_uuid=str(uuid.uuid5(_TRIAL_CONVERTED_NAMESPACE, f"{account['owner_id']}:{event_data['id']}")),
        )

    # Refresh the owner-facts file (plan/provider snapshot the bundled isol8
    # skill points agents at). Subscription-state flips land HERE — not via
    # change_provider — so without this write a past_due/canceled transition
    # would leave agents reporting an active paid plan. The just-persisted
    # status (and the backup-path provider, when written) ride along as
    # overrides — same-process pinning that outranks any row read.
    # write_owner_facts is internally best-effort.
    from core.services.owner_facts import write_owner_facts

    facts_overrides: dict = {"subscription_status": new_status}
    if provider_persisted:
        # Only claim the metadata provider when its billing-row write actually
        # committed — a swallowed set_provider_choice failure above must not
        # produce a facts file asserting a provider the row never got.
        facts_overrides["provider_choice"] = metadata_provider
        facts_overrides["byo_provider"] = metadata_byo if metadata_provider == "byo_key" else None
    if plan_persisted:
        facts_overrides["plan"] = new_plan
    await write_owner_facts(account["owner_id"], account_overrides=facts_overrides)

    # Customer-analytics bridge (the EMF put_metric above is the operational
    # counterpart) — land the lifecycle transition on the person. Metadata
    # only rides along when its billing-row write committed; otherwise the
    # bridge falls back to the row's provider, so a skipped backup (live→live
    # recovery) or a swallowed write failure never stamps the person profile
    # with a provider the row doesn't have.
    _bridge_subscription_state(
        account, event_data, old_status, new_status, metadata_provider if provider_persisted else None
    )

    if reconcile_needed:
        # Make the runtime match the billing just committed: a trial->paid conversion (the
        # workspace still runs the trial provider in every profile), a re-subscribe, a
        # provider/plan change -- and any live event at all, because a REDELIVERY of a
        # conversion finds old_status already "active" (entering_live False, no plan diff) and
        # must still reach the reconcile. That costs one idempotent reconcile per renewal event.
        # ``reconcile_runtime`` re-derives the owner role and gate token, moves the OpenRouter key
        # onto the paid limit, rewrites every profile, ensures the Metronome customer and re-arms
        # the nightly CEO. ORDER: every billing write above has committed, and the reconcile
        # reads billing strongly consistent, so it never renders the trial shape.
        #
        # A dispatch that could not start RAISES: the router drops the dedup row and 500s, so
        # Stripe redelivers. Every step above is idempotent under redelivery (see the slice 3
        # report): the set_* writes rewrite the same values, entering_live side effects no longer
        # fire (old_status is now live), trial_converted dedupes on its uuid.
        from core.services.provision_dispatch import dispatch_reconcile

        if not await dispatch_reconcile(account["owner_id"]):
            raise RuntimeError(f"reconcile dispatch failed for owner {account['owner_id']}; Stripe will redeliver")
    if new_status in ("active", "trialing"):
        # Seats follow the roster (T04). LAST, and a failure RAISES so Stripe redelivers: every
        # step above is idempotent and the sync sets an absolute quantity. Our own seat modify
        # fires .updated, which re-syncs against a fresh roster, so racing membership events
        # converge.
        from core.services.billing_service import sync_seat_quantity

        try:
            await sync_seat_quantity(account["owner_id"])
        except Exception:
            put_metric("billing.seat_sync", dimensions={"outcome": "error"})
            logger.exception("Seat sync failed for owner %s; Stripe will redeliver", account["owner_id"])
            raise
    # The Front desk line follows the item, for every status (a canceled subscription releases it).
    # sync_front_desk decides on the fresh subscription; a stale cancel of a superseded one is skipped
    # there, against the id stored before this event's own write (``stored_sub``) and the id stored now.
    # Only a buyer is synced: with no item on the event and no claim on the row there is nothing to buy
    # or release.
    if account.get("front_desk") or any(
        _is_front_desk_item(item or {}) for item in (event_data.get("items") or {}).get("data") or []
    ):
        from core.services.text_lines import sync_front_desk

        await sync_front_desk(
            account["owner_id"], subscription_id=event_data["id"], previous_subscription_id=stored_sub
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
    # The texting line goes through the same rule as every other event (sync_front_desk): a stale or
    # superseded subscription being deleted never takes the line from a live one. Gated on the claim
    # alone, since a deleted subscription's items are gone. Before cancel_subscription's own write, so
    # the stored id is the one this event found. A failure raises so Stripe redelivers.
    if account.get("front_desk"):
        from core.services.text_lines import sync_front_desk

        await sync_front_desk(
            account["owner_id"],
            subscription_id=event_data["id"],
            previous_subscription_id=account.get("stripe_subscription_id"),
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
    from core.billing.voice_plan_payments import on_invoice

    await on_invoice(event)
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
    from core.billing.voice_plan_payments import on_invoice

    await on_invoice(event)
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


async def handle_checkout_session(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """Handle the Checkout events still owned outside subscription lifecycle.

    Subscription state is handled exclusively by ``customer.subscription.*``.
    Customer credit purchases use Metronome payment-gated commits directly;
    there is no Stripe Checkout credit-settlement or bundled-credit branch.
    """
    from core.billing.voice_plan_checkout import complete

    if await complete(event):
        return Response(status_code=200)
    session = event["data"]["object"]
    session_metadata = session.get("metadata") or {}
    # Launch competition: a settled session that carried the winner free-month
    # discount just CONSUMED the coupon — stamp the sheet row so a forced
    # close rerun cannot attach a second free month to the subscription this
    # checkout minted. This check happens before the handler returns because
    # most winner checkouts carry no credits at all. The settled check is
    # load-bearing, not defensive: a delayed-payment session fires
    # checkout.session.completed while the payment can still FAIL, and
    # stamping there burns the winner's free month without ever giving it to
    # them. async_payment_succeeded lands back here and stamps it then.
    # Best-effort — never raises.
    if (
        session_metadata.get("launch_winner_coupon") == "true"
        and session_metadata.get("owner_id")
        and session.get("payment_status") in _SETTLED_PAYMENT_STATUSES
    ):
        from core.services.launch_close import mark_winner_coupon_consumed

        await mark_winner_coupon_consumed(session_metadata["owner_id"])
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
    "invoice.payment_action_required": handle_invoice_payment_failed,
    "invoice.paid": handle_invoice_paid,
    "checkout.session.completed": handle_checkout_session,
    "checkout.session.async_payment_succeeded": handle_checkout_session,
}
