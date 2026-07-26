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
from core.services import webhook_dedup
from core.services.billing_service import BillingService, subscription_item_on_price

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
#     so the poll would dispatch its OWN dispatch_provision — with no drain
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
    already on the row when the next ~3s poll reads it.

    A SCOPED write (``update_conversion_fields``), never the get→put
    ``update_fields``: every conversion-marker write can race a concurrent
    provision claim, and a full-item put built from a pre-claim read
    resurrects the stale status and erases the lease — see that helper's
    docstring for the live incident this closed."""
    await container_repo.update_conversion_fields(
        owner_id,
        {
            "conversion_pending": True,
            "substatus": CONVERTING_SUBSTATUS,
            "conversion_started_at": utc_now_iso(),
        },
    )


def _conversion_entry_key(owner_id: str, subscription_id: str) -> str:
    """Dedup key for the conversion's single-flight entry CAS. Keyed on
    (owner, Stripe subscription) — i.e. per subscription TRANSITION, since a
    given subscription converts an owner off trial_local at most once. A
    later re-subscribe is a new subscription id, so it gets a fresh key."""
    return f"trial-conversion:{owner_id}:{subscription_id}"


async def _claim_conversion_entry(owner_id: str, subscription_id: str) -> bool:
    """Single-flight the conversion ENTRY across concurrently delivered
    Stripe events. Returns True iff THIS event may run the conversion.

    Stripe delivers several events per subscription change (.created,
    .updated, another .updated from metadata stamping, ...), UNORDERED and
    near-simultaneously — possibly to two backend tasks during a rolling
    deploy. Each handler reads the billing row's old_status before any of
    them persists the new one, so more than one event can see
    ``trial_local -> active`` and enter the conversion branch. Two live
    conversions then interleave stop / dispatch / release writes on the same
    container row — the observed dev strand: the second attempt's
    writes superseded the first attempt's SFN execution while landing none of
    its own.

    The gate is a conditional PutItem on the existing webhook-dedup table
    (``webhook_dedup.record_event_or_skip`` — reused rather than a new
    store): DynamoDB serializes it across all backend tasks, so exactly one
    event records the key and every other finds it and no-ops.

    Fail OPEN on infrastructure errors: if the dedup write itself blows up
    (DDB blip, misconfig), running the conversion — protected one layer down
    by the provision lease's own single-flight — beats silently stranding a
    paying owner on the trial box with nothing left to re-fire it (Stripe's
    retry re-enters this branch only while old_status is still trial_local).
    """
    try:
        result = await webhook_dedup.record_event_or_skip(
            _conversion_entry_key(owner_id, subscription_id),
            source="stripe-trial-conversion",
        )
    except Exception:  # noqa: BLE001 — fail open, see docstring
        logger.exception(
            "Conversion-entry CAS failed for owner %s (sub %s) — proceeding un-deduped",
            owner_id,
            subscription_id,
        )
        return True
    if result is webhook_dedup.WebhookDedupResult.ALREADY_SEEN:
        put_metric("billing.trial_conversion.duplicate_entry")
        logger.info(
            "Skipping duplicate trial-conversion entry for owner %s (sub %s) — another event owns it",
            owner_id,
            subscription_id,
        )
        return False
    return True


async def _rollback_conversion_entry(owner_id: str, subscription_id: str) -> None:
    """Drop the conversion-entry CAS row after a failed stamp/dispatch, so a
    sibling event (or a Stripe redelivery) can re-enter the branch instead of
    finding the claim of a conversion that never actually started. Best-effort:
    if the delete fails the row's 30-day TTL is the backstop, and the
    ``conversion_pending`` self-heal covers any conversion that got as far as
    the stamp."""
    try:
        await webhook_dedup.delete_event(_conversion_entry_key(owner_id, subscription_id))
    except Exception:  # noqa: BLE001 — rollback is best-effort
        logger.warning(
            "Could not roll back conversion-entry CAS for owner %s (sub %s)",
            owner_id,
            subscription_id,
            exc_info=True,
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

    # SCOPED release (``update_conversion_fields``), never the get→put
    # ``update_fields``. This write races the provision claim of the dispatch
    # task ``reprovision_for_user`` just created: the old full-item put here,
    # built from a read taken before that claim landed, resurrected
    # status="stopped" over the fresh "provisioning" and erased the lease —
    # superseding the conversion's OWN SFN execution at its first checkpoint
    # and stranding the row with nothing running and nothing scheduled (the
    # #1428 dev incident's terminal state: stopped, lease-less, last_error
    # null). The scoped write touches only the marker fields, so the claim —
    # and the execution behind it — survive.
    fields: dict[str, Any] = {"substatus": None}
    if succeeded:
        fields["conversion_pending"] = False
    try:
        await container_repo.update_conversion_fields(owner_id, fields)
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
        # dispatch_provision moving the row off "stopped".
        #
        # EXCEPT for the stranded shape: stopped, NO provision claim, NO
        # recorded error. A healthy hand-off leaves either a claim (the
        # dispatch won it — an SFN execution owns the row) or, transiently,
        # this exact shape for the milliseconds before the claim lands. But a
        # conversion whose dispatched execution was superseded with no
        # successor sits in it PERMANENTLY — nothing running, nothing
        # scheduled — and honouring the guard here would stand the poll's
        # wake path down for the full window on a row nobody owns. Release
        # the guard instead: the caller's wake branch re-checks every gate
        # (paid, trial, parked) and its dispatch is single-flight
        # (try_claim_provision), so the worst case in the healthy-hand-off
        # millisecond window is one redundant dispatch that loses the claim
        # and stands down. The drain window itself is NOT exposed —
        # conversion_pending is still True there, which keeps the branch
        # above this one.
        if (
            guard
            and container.get("status") == "stopped"
            and not container.get("provision_claim_id")
            and not container.get("last_error")
        ):
            put_metric("billing.trial_conversion.stranded_row")
            logger.warning(
                "Conversion left owner %s stopped with no provision claim and no error — "
                "releasing the conversion guard so the status poll can wake the row",
                owner_id,
            )
            return False
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
            # auto-retry + provision gate handle the row. Scoped write — the
            # same lease-safety rule as every other marker write here.
            await container_repo.update_conversion_fields(owner_id, {"conversion_pending": False})
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


async def _count_owner_paperclip_agents(owner_id: str) -> int:
    """Live Paperclip agent count for an owner, or 0 if it can't be fetched.

    Constructs the httpx client + repo + admin_client the same way
    ``agent_export`` and ``ecs_manager`` do, then delegates to
    ``fetch_paperclip_agents_for_owner`` (which returns [] — never raises — when
    the owner has no company yet or Paperclip is unreachable). Best-effort: any
    construction failure degrades to 0.
    """
    import httpx

    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        admin_client = PaperclipAdminClient(http_client=http)
        agents = await fetch_paperclip_agents_for_owner(owner_id=owner_id, repo=repo, admin_client=admin_client)
        return len(agents)
    except Exception:
        logger.exception("Failed to count Paperclip agents for owner %s", owner_id)
        return 0
    finally:
        await http.aclose()


async def _apply_ultra_resize(owner_id: str, *, old_plan: str, new_plan: str) -> None:
    """Resize the owner's container to match a tier TRANSITION.

    No-op unless the tier actually flipped — every ordinary ``.updated`` (renewal,
    payment-method change, seat sync) must not bounce the container.

    - Upgrade (→ ultra_pro): grow to the Ultra box.
    - Downgrade (ultra_pro → standard): shrink to the flat box ONLY when the
      live agent count is in ``0 < count <= COMPANY_STANDARD_PLAN_MAX_AGENTS``.
      The count is DEFERRED (container left at Ultra size, metric emitted) when:
        * ``count > COMPANY_STANDARD_PLAN_MAX_AGENTS`` — an oversized company;
          the smaller box would OOM under the same workload.
        * ``count == 0`` — the count is UNDETERMINABLE. Every provisioned owner
          always has Friday + Atlas seeded, so a reachable owner's list is never
          empty (>= 2); 0 means Paperclip was unreachable, NOT a legitimately
          tiny owner. Shrinking on an unknown count would OOM an oversized owner
          and NOT self-heal (post-shrink the plan is "standard", so later
          flat-price events are standard->standard no-ops — only an admin resize
          or a paid re-upgrade recovers). Defer instead.
      Billing follows Stripe either way (the plan is already persisted
      "standard"); only the resize waits for the owner to trim agents (a later
      sub event or an admin completes the shrink). This is the OOM-safe
      downgrade.

    Best-effort: a resize failure must not 500 the webhook back to Stripe.
    """
    was_ultra = old_plan == "ultra_pro"
    now_ultra = new_plan == "ultra_pro"
    if was_ultra == now_ultra:
        return  # no tier transition — never resize on a plain .updated

    from core.containers import get_ecs_manager
    from core.containers.ecs_manager import (
        PER_USER_CPU,
        PER_USER_MEMORY_MIB,
        ULTRA_CPU,
        ULTRA_MEMORY_MIB,
    )
    from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS

    try:
        if now_ultra:
            await get_ecs_manager().resize_user_container(owner_id, new_cpu=ULTRA_CPU, new_memory=ULTRA_MEMORY_MIB)
            put_metric("ultra.resize", dimensions={"direction": "up"})
            return

        # Downgrade out of Ultra: shrink DOWN only for a confirmed-small company
        # (0 < count <= budget). Defer otherwise so we never OOM an oversized —
        # or an undeterminable — owner into the smaller box.
        agent_count = await _count_owner_paperclip_agents(owner_id)
        if agent_count == 0:
            # A reachable owner ALWAYS lists >= 2 agents (Friday + Atlas are
            # seeded at provision), so 0 means the count is unavailable
            # (Paperclip unreachable), never a genuinely tiny owner. Shrinking on
            # an unknown count risks OOMing an oversized owner AND doesn't
            # self-heal (post-shrink plan="standard" makes later flat events
            # standard->standard no-ops). Defer to an admin / re-upgrade.
            put_metric("ultra.downgrade_deferred_unknown_count", 1)
            logger.warning(
                "Deferring Ultra down-resize for owner %s: agent count unavailable "
                "(Paperclip unreachable) — leaving container at Ultra size to avoid OOM",
                owner_id,
            )
            return
        if agent_count > COMPANY_STANDARD_PLAN_MAX_AGENTS:
            put_metric("ultra.downgrade_deferred_oversized", 1)
            logger.warning(
                "Deferring Ultra down-resize for owner %s: %d agents exceed the "
                "standard budget of %d — leaving container at Ultra size to avoid OOM",
                owner_id,
                agent_count,
                COMPANY_STANDARD_PLAN_MAX_AGENTS,
            )
            return

        await get_ecs_manager().resize_user_container(owner_id, new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB)
        put_metric("ultra.resize", dimensions={"direction": "down"})
    except Exception:
        logger.exception("Ultra tier resize failed for owner %s", owner_id)


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
    if new_status in ("incomplete", "incomplete_expired") and old_status in ("trial_local", "active", "trialing"):
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
    # Events on an already-established subscription (renewals, seat syncs,
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
    if entering_live and metadata_provider in ("chatgpt_oauth", "byo_key", "bedrock_claude"):
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
    # A Teams subscription carries TWO items (flat base + per-seat) and Stripe
    # does not guarantee their order. Select the seat item BY PRICE ID: storing
    # the base item's id here would make sync_team_seat_quantity drive the base
    # quantity, billing a 4-seat org $50x4 + $20 instead of $50 + $20x4.
    # NOTHING in this handler reads items by position — see is_ultra below.
    seat_item = subscription_item_on_price(items, settings.STRIPE_TEAM_SEAT_PRICE_ID)
    # bool(seat_item) is a strict superset of the old "first item is the seat
    # price" fallback, and cannot over-detect: nothing but a real per-seat line
    # item ever carries STRIPE_TEAM_SEAT_PRICE_ID.
    is_team_seat = metadata.get("purpose") == "team_seat" or bool(seat_item)
    team_seat_persisted = False
    if is_team_seat and seat_item:
        try:
            await billing_repo.set_team_subscription(
                owner_id=account["owner_id"],
                subscription_id=event_data["id"],
                subscription_item_id=seat_item.get("id"),
                status=new_status,
                seat_count=int(seat_item.get("quantity") or 0),
            )
            team_seat_persisted = True
        except Exception:
            logger.exception("Failed to record team seat state for owner %s", account["owner_id"])
    elif is_team_seat and not seat_item:
        # Reached only via metadata.purpose == "team_seat" (bool(seat_item)
        # can't be True here without seat_item also being truthy) but no item
        # on the subscription matches STRIPE_TEAM_SEAT_PRICE_ID -- either the
        # env var is unset in this deploy or the Stripe seat price was rotated
        # to a new id. Non-destructive (existing stored subscription_item_id/
        # seat_count are left alone), but must not be silent: a rotation would
        # otherwise freeze seat counts on every subsequent webhook with no
        # visible signal.
        logger.warning(
            "Team-seat subscription %s (owner %s) detected via metadata but no "
            "item matches STRIPE_TEAM_SEAT_PRICE_ID=%r -- seat state not refreshed "
            "(price unset or rotated?)",
            event_data.get("id"),
            account["owner_id"],
            settings.STRIPE_TEAM_SEAT_PRICE_ID,
        )

    # Ultra Pro tier: derive the tier FROM THE PRICE on the subscription item —
    # a direct sibling of the team-seat detection above, and NEVER from
    # subscription.metadata (a checkout-time snapshot that goes stale; the same
    # clobber the entering_live provider guard defends against). Persist the
    # plan and resize the container only on the tier transition.
    # Scanned by price, not read off items[0]: an Ultra sub is single-item
    # TODAY, but the moment one grows a second line item (a metered-overage
    # item, a Stripe-side add-on or coupon) an Ultra owner whose ultra price
    # lands at index >= 1 would fall to "standard" here and get their
    # container resized DOWN — silently. Same rule as the seat item above.
    is_ultra = bool(subscription_item_on_price(items, settings.STRIPE_ULTRA_PRICE_ID))
    if items and not is_team_seat:
        # Only act when the payload actually carries the subscription item. A
        # partial `.updated` can arrive with an empty items.data; treating that
        # absence as "not ultra" would wrongly downgrade + shrink an Ultra owner.
        # Team-seat subs are excluded outright — their per-seat pricing is a
        # different axis (never the ultra price), and their container sizing is
        # managed by the seat flow, not the tier flow.
        old_plan = account.get("plan") or "standard"
        new_plan = "ultra_pro" if is_ultra else "standard"
        plan_persisted = False
        try:
            await billing_repo.set_plan(account["owner_id"], plan=new_plan, now_iso=utc_now_iso())
            plan_persisted = True
        except Exception:
            logger.exception("Failed to persist plan for owner %s", account["owner_id"])
        # Resize ONLY when the durable plan write committed. If set_plan raised
        # (transient DDB) but we resized anyway, the container size would change
        # without the plan persisted — plan and size diverge, and a later
        # flat-price event would read the OLD plan and mis-decide the resize.
        # Skipping the resize keeps them from ever splitting in the
        # size-without-plan direction. Resize decisions still key off the row's
        # PRIOR plan vs the price-derived one, so it fires only on a real
        # transition even though the persist is idempotent. Best-effort inside
        # (a resize failure never 500s the webhook).
        if plan_persisted:
            await _apply_ultra_resize(account["owner_id"], old_plan=old_plan, new_plan=new_plan)

    if old_status == "trial_local" and new_status in ("trialing", "active"):
        # Trial→paid conversion: the running container is still the locked-down
        # trial box. We must go through reprovision_for_user, NOT a bare
        # dispatch_provision: for a RUNNING container provision_user_container
        # only issues forceNewDeployment (no write_user_configs, no task-def
        # re-register), so the trial config would survive the conversion.
        # reprovision_for_user scales to zero + drains first, forcing the
        # follow-on dispatch_provision down the scale-from-zero branch that
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
        #
        # SINGLE-FLIGHT entry: the whole block is gated on a CAS keyed
        # on (owner, subscription id). Stripe delivers several events per
        # subscription change and their handlers race the billing-row read
        # above, so more than one can see old_status == "trial_local" and land
        # here — two live conversions then interleave stop/dispatch/release
        # writes on the same container row. Exactly one event claims the entry;
        # the rest no-op it entirely (stamp, dispatch AND the trial_converted
        # capture — it is the same transition, once). See
        # _claim_conversion_entry for the full failure narrative.
        if await _claim_conversion_entry(account["owner_id"], event_data["id"]):
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
                        # Same persisted-only rule as the bridge below: claim the
                        # metadata provider only when its row write committed.
                        "provider_choice": (metadata_provider if provider_persisted else None)
                        or account.get("provider_choice"),
                    },
                )
            except Exception:
                logger.exception(
                    "Trial-conversion reprovision dispatch failed for owner %s",
                    account["owner_id"],
                )
                # Free the entry claim so a sibling event / Stripe redelivery
                # can retry a conversion that never got as far as the stamp.
                # (If the stamp DID land, the conversion_pending self-heal is
                # the recovery path either way.)
                await _rollback_conversion_entry(account["owner_id"], event_data["id"])

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
    if team_seat_persisted:
        # Same shape for the seat fields: on a brand-new Teams seat sub the
        # eventually-consistent row re-read can miss the just-committed
        # subscription_item_id/seat_count and mislabel the plan as flat-fee.
        facts_overrides["subscription_item_id"] = seat_item.get("id")
        facts_overrides["seat_count"] = int(seat_item.get("quantity") or 0)
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

    # Refresh the owner-facts file before teardown: teardown is best-effort
    # and may be delayed or fail, and a still-running container's agents must
    # not keep reporting an active paid plan. Status rides as an override —
    # same-process pinning that outranks any row read. Internally best-effort.
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
