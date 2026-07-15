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

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import stripe
from fastapi import Response

from core.config import settings
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric, timing
from core.observability.product_analytics import capture_product_event
from core.repositories import billing_repo, container_repo
from core.services.billing_service import BillingService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trial -> paid conversion: serialization + self-heal
# ---------------------------------------------------------------------------
#
# Lives here (rather than in routers/container.py) because the webhook OWNS the
# conversion; /container/status is only its second reader. The status path
# imports `maybe_resume_stuck_conversion` from this module.
#
# Two failure modes this section closes:
#
# (a) DOUBLE PROVISION. `reprovision_for_user` scales the trial container to
#     zero and then BLOCKS in a ~90s drain wait — deliberately: the per-owner
#     state is WAL-mode SQLite on EFS and two live tasks holding it open is our
#     documented corruption mode. The just-paid owner is meanwhile polling
#     GET /container/status every ~3s, and that endpoint auto-retries an
#     ("error", "stopped") row. Now that the owner is paid, no gate blocks it,
#     so the poll would dispatch its OWN background_provision — with no drain
#     wait — right on top of the draining task. So the row is STAMPED before
#     the stop and the status path stands down while the stamp is live.
#
#     The stamp is a TIMESTAMP, not just the substatus, and that is load-
#     bearing: `ecs_manager.stop_user_service` writes
#     `update_status(owner, "stopped")` whose substatus defaults to None, so it
#     clobbers `substatus="converting"` the instant the drain begins. The
#     substatus is the human-readable label (and what the UI renders);
#     `conversion_started_at` is what actually holds the auto-retry off.
#
# (b) STUCK CONVERSION. The reprovision is one fire-and-forget asyncio task. If
#     it dies (backend deploy mid-drain, an ECS error, a raise anywhere),
#     nothing re-fires it: Stripe's retry sees `old_status != "trial_local"`
#     and skips the branch entirely, so a PAYING customer keeps the trial
#     container — and the trial cap — forever. `conversion_pending` survives
#     until a reprovision is actually handed off, and the status path re-fires
#     it once the guard window lapses.

CONVERTING_SUBSTATUS = "converting"

# How long a conversion owns the container row before the status path treats it
# as dead and re-fires it. Doubles as the self-heal's rate limit — the poll runs
# every ~3s, so an unguarded re-dispatch would be a reprovision storm.
#
# Sized off the longest HEALTHY dispatch: stop (~1s) + the drain wait (90s cap)
# + the create_task hand-off — i.e. ~2 min worst case. 5 minutes clears that
# with room to spare, and bounds how long a paying customer can sit on a dead
# conversion.
CONVERSION_GUARD_SECONDS = 300.0

# Subscription statuses that mean "this owner is paying" — the precondition for
# the self-heal. (`trialing` is a card-collected Stripe trial, not the no-card
# local `trial_local`.)
_PAID_SUBSCRIPTION_STATUSES = ("trialing", "active")


def _within_guard_window(started_at: str | None) -> bool:
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        # Unparseable stamp: treat as absent rather than wedging the row.
        logger.warning("Unparseable conversion_started_at %r", started_at)
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() < CONVERSION_GUARD_SECONDS


def conversion_guard_active(container: dict | None) -> bool:
    """True while a trial->paid conversion owns this container row.

    Callers must not provision/restart the container while this holds — see (a).

    The TIMESTAMP is the guard, not the substatus. ``_stamp_conversion_dispatch``
    writes both, but ``stop_user_service`` wipes ``substatus`` unconditionally a
    second into the drain, so the substatus is unreliable in the one direction
    that matters — and treating it as guard-active on its own is unbounded: a
    conversion that dies after stamping would leave the row permanently guarded,
    freezing the status-path auto-retry AND every start path, stranding a PAYING
    customer on a trial container with only a manual admin lever to free them.
    The window is what makes the guard self-releasing.
    """
    if not container:
        return False
    return _within_guard_window(container.get("conversion_started_at"))


async def _stamp_conversion_dispatch(owner_id: str) -> None:
    """Claim the container row for a conversion. Awaited BEFORE every dispatch
    (webhook and self-heal alike) so the marker is durable even if the
    fire-and-forget task never gets to run, and so the rate-limit anchor is
    already on the row when the next ~3s poll reads it."""
    await container_repo.update_fields(
        owner_id,
        {
            "conversion_pending": True,
            "substatus": CONVERTING_SUBSTATUS,
            "conversion_started_at": utc_now_iso(),
        },
    )


async def run_conversion_reprovision(owner_id: str) -> None:
    """Reprovision a converted owner's container off the trial shape, then
    release the row. asyncio.create_task target — never raises.

    Release semantics, both paths:
      - success: substatus cleared AND `conversion_pending` dropped. The
        container has been scaled to zero, drained, and a fresh provision
        dispatched; from here the ordinary auto-retry does the right thing
        (its scale-from-zero branch re-registers the task def and rewrites
        openclaw.json from the now-paid provider_choice).
      - failure: substatus cleared — never strand the row in a state nothing
        acts on — but `conversion_pending` DELIBERATELY survives. It is the
        only record that this owner is paying for a trial-shaped container,
        and it is what `maybe_resume_stuck_conversion` re-fires off.
    """
    from core.containers import get_ecs_manager

    succeeded = False
    try:
        await get_ecs_manager().reprovision_for_user(owner_id)
        succeeded = True
    except Exception:
        logger.exception("Trial-conversion reprovision failed for owner %s", owner_id)
    put_metric(
        "billing.trial_conversion.reprovision",
        dimensions={"outcome": "ok" if succeeded else "error"},
    )

    fields: dict[str, Any] = {"substatus": None}
    if succeeded:
        fields["conversion_pending"] = False
    try:
        await container_repo.update_fields(owner_id, fields)
    except Exception:
        # The stamp lapses on its own (the guard window is time-boxed), so a
        # failed release costs at most one redundant self-heal — never a wedge.
        logger.exception("Failed to release the conversion marker for owner %s", owner_id)


async def maybe_resume_stuck_conversion(owner_id: str, container: dict | None) -> bool:
    """Status-path hook. Returns True when a trial->paid conversion owns this
    row — in flight, or just re-dispatched here — meaning the caller must NOT
    auto-retry/restart the container underneath it.

    Costs nothing on the hot path: a row with no conversion marker returns
    False after two dict reads, with no DynamoDB call at all.
    """
    if not container:
        return False

    guard = conversion_guard_active(container)
    if not container.get("conversion_pending"):
        # No pending conversion. `guard` is still honoured: it covers the brief
        # window between a successful hand-off (which drops the flag) and
        # background_provision moving the row off "stopped".
        return guard
    if guard:
        # In flight, or re-dispatched within the guard window. Hands off — this
        # is the rate limit that keeps the ~3s poll from storming reprovisions.
        return True

    # Pending + the window lapsed == the dispatch died. Stripe will never
    # re-fire it. Re-dispatch, but only for an owner who is actually paying.
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        status = (account or {}).get("subscription_status")
        if status not in _PAID_SUBSCRIPTION_STATUSES:
            # Not (or no longer) paid — e.g. the subscription was canceled
            # before the conversion landed. Drop the flag and let the ordinary
            # auto-retry + provision gate handle the row.
            await container_repo.update_fields(owner_id, {"conversion_pending": False})
            return False

        await _stamp_conversion_dispatch(owner_id)
        asyncio.create_task(run_conversion_reprovision(owner_id))
        container["substatus"] = CONVERTING_SUBSTATUS  # keep the caller's response honest
        put_metric("billing.trial_conversion.self_heal")
        logger.warning(
            "Re-dispatching a stuck trial-conversion reprovision for owner %s (subscription=%s)",
            owner_id,
            status,
        )
        return True
    except Exception:
        # Fail CLOSED: a DDB blip must not let the auto-retry provision on top
        # of a conversion that may still be draining. The next poll retries.
        logger.exception("Stuck-conversion self-heal failed for owner %s", owner_id)
        return True


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


def _subscriber_distinct_id(account: dict, event_data: dict) -> str | None:
    """Clerk USER id to key PostHog events on, so server-side billing events
    merge onto the same person the frontend ``identify()``s. Personal owners:
    ``owner_id`` IS the Clerk user id. Org owners: ``owner_id`` is the org id,
    so prefer the subscribing human's ``clerk_user_id`` threaded through
    checkout metadata (``create_flat_fee_checkout`` sets it)."""
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
    elif new_status == "active" and old_status == "trialing":
        event = "trial_converted"  # the trial->paid moment — the core SaaS metric
    elif new_status == "active":
        event = "subscription_activated"
    elif new_status == "past_due":
        event = "subscription_past_due"
    else:
        event = "subscription_status_changed"
    capture_product_event(
        distinct_id=_subscriber_distinct_id(account, event_data),
        event=event,
        properties={"provider_choice": provider, "from_status": old_status, "to_status": new_status},
        set_person=set_person,
    )


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
    new_status = event_data.get("status", "active")
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
            provider_persisted = True
        except Exception:
            logger.exception(
                "Failed to persist provider_choice for owner %s",
                account["owner_id"],
            )
    # Teams per-seat subscriptions additionally record the subscription item
    # id + seat quantity so sync_team_seat_quantity has an item to modify.
    # Detect by explicit metadata purpose OR by the seat price id. Personal
    # flat subs skip this entirely.
    items = (event_data.get("items") or {}).get("data") or []
    first_item = items[0] if items else {}
    first_price_id = (first_item.get("price") or {}).get("id")
    is_team_seat = metadata.get("purpose") == "team_seat" or (
        bool(settings.STRIPE_TEAM_SEAT_PRICE_ID) and first_price_id == settings.STRIPE_TEAM_SEAT_PRICE_ID
    )
    team_seat_persisted = False
    if is_team_seat and first_item:
        try:
            await billing_repo.set_team_subscription(
                owner_id=account["owner_id"],
                subscription_id=event_data["id"],
                subscription_item_id=first_item.get("id"),
                status=new_status,
                seat_count=int(first_item.get("quantity") or 0),
            )
            team_seat_persisted = True
        except Exception:
            logger.exception("Failed to record team seat state for owner %s", account["owner_id"])

    if old_status == "trial_local" and new_status in ("trialing", "active"):
        # Trial→paid conversion: the running container is still the locked-down
        # trial box. We must go through reprovision_for_user, NOT a bare
        # background_provision: for a RUNNING container provision_user_container
        # only issues forceNewDeployment (no write_user_configs, no task-def
        # re-register), so the trial config would survive the conversion.
        # reprovision_for_user scales to zero + drains first, forcing the
        # follow-on background_provision down the scale-from-zero branch that
        # re-registers the task def AND rewrites openclaw.json — genuinely
        # turning the trial box into a paid one. The status was already
        # persisted off "trial_local" above, so by the time this runs
        # resolve_provider_choice reads the stored paid provider_choice. The
        # reprovision costs the owner a few minutes of container downtime
        # (drain + image pull) right after paying — unavoidable, since only a
        # cold roll lifts the trial config.
        # Best-effort: the status persist above already committed, so a raise
        # here would 500 the webhook and Stripe's retry would see
        # old_status != "trial_local" and never re-fire this branch — log and
        # continue instead of dropping the reprovision behind a 500.
        #
        # The stamp is AWAITED here, inside the webhook, before the dispatch:
        #   - it must land before reprovision_for_user scales to zero, or the
        #     owner's ~3s /container/status poll auto-retries the "stopped" row
        #     underneath the drain wait and races two tasks onto the EFS WAL;
        #   - `conversion_pending` must be durable even if the fire-and-forget
        #     task below never runs (deploy, crash), because Stripe's retry
        #     will never re-enter this branch. That flag is the only thing that
        #     lets the status path self-heal the conversion.
        try:
            await _stamp_conversion_dispatch(account["owner_id"])
            asyncio.create_task(run_conversion_reprovision(account["owner_id"]))
            # Trial funnel telemetry: the free-tier trial converting to
            # paid billing — distinct from the generic trialing->active
            # "trial_converted" the bridge below may also emit for a REAL
            # (card-collected) Stripe trial; this one fires specifically off
            # the local no-card trial_local start state.
            capture_product_event(
                distinct_id=_subscriber_distinct_id(account, event_data),
                event="trial_converted",
                properties={
                    "from_status": old_status,
                    "to_status": new_status,
                    "provider_choice": metadata_provider or account.get("provider_choice"),
                },
            )
        except Exception:
            logger.exception(
                "Trial-conversion reprovision dispatch failed for owner %s",
                account["owner_id"],
            )

    # Refresh the owner-facts file (plan/provider snapshot the bundled isol8
    # skill points agents at). Subscription-state flips land HERE — not via
    # change_provider — so without this write a past_due/canceled transition
    # would leave agents reporting an active paid plan. The just-persisted
    # status (and the backup-path provider, when written) ride along as
    # overrides because the row re-read is eventually consistent.
    # write_owner_facts is internally best-effort.
    from core.services.owner_facts import write_owner_facts

    facts_overrides: dict = {"subscription_status": new_status}
    if provider_persisted:
        # Only claim the metadata provider when its billing-row write actually
        # committed — a swallowed set_provider_choice failure above must not
        # produce a facts file asserting a provider the row never got.
        facts_overrides["provider_choice"] = metadata_provider
        facts_overrides["byo_provider"] = metadata_byo if metadata_provider == "byo_key" else None
    if team_seat_persisted:
        # Same shape for the seat fields: on a brand-new Teams seat sub the
        # eventually-consistent row re-read can miss the just-committed
        # subscription_item_id/seat_count and mislabel the plan as flat-fee.
        facts_overrides["subscription_item_id"] = first_item.get("id")
        facts_overrides["seat_count"] = int(first_item.get("quantity") or 0)
    await write_owner_facts(account["owner_id"], account_overrides=facts_overrides)

    # Customer-analytics bridge (the EMF put_metric above is the operational
    # counterpart) — land the lifecycle transition on the person.
    _bridge_subscription_state(account, event_data, old_status, new_status, metadata_provider)
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
            properties={"trial_end": event_data.get("trial_end")},
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
            "was_trialing": account.get("subscription_status") == "trialing",
            "provider_choice": account.get("provider_choice"),
            "cancel_at_period_end": bool(event_data.get("cancel_at_period_end")),
        },
        set_person={"subscription_status": "canceled", "is_subscribed": False},
    )
    await billing_service.cancel_subscription(account)
    logger.info("Subscription cancelled for owner %s", account["owner_id"])

    # Refresh the owner-facts file before teardown: teardown is best-effort
    # and may be delayed or fail, and a still-running container's agents must
    # not keep reporting an active paid plan. Status rides as an override
    # because the row re-read is eventually consistent. Internally best-effort.
    from core.services.owner_facts import write_owner_facts

    await write_owner_facts(account["owner_id"], account_overrides={"subscription_status": "canceled"})

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
