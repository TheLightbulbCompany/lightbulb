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
#     so the poll would dispatch its OWN dispatch_provision — with no drain
#     wait — right on top of the draining task. So the row is STAMPED before
#     the stop and the status path stands down while the stamp is live.
#
#     The stamp is a TIMESTAMP, not just the substatus, and that is load-
#     bearing: a stop writes
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

# Provision-gate codes the PROVISIONER itself cannot survive: it pre-stages the
# ChatGPT token (``_pre_stage_oauth_for_user`` raises without one) and needs
# the BYO sub-choice before it can reference a key. Dispatching a conversion
# reprovision past one of these can only end in a failed provision — and the
# owner reaches that state legitimately: the ChatGPT device-code flow only
# STARTS after checkout, so the Stripe webhook lands minutes before the tokens
# do. Every other gate (credits, past-due, subscription) is UX on a box that
# can still be built, and the conversion must not leave a paying owner on the
# trial shape over it.
_CONVERSION_PREREQUISITE_GATES = frozenset({"oauth_required", "byo_provider_required"})

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
    # A DEFERRED conversion (``conversion_deferred_reason`` set) never stopped
    # the container — nothing is draining, so there is nothing for recover /
    # start to stand down for. Its stamp is kept only as the self-heal's
    # rate-limit anchor (``maybe_resume_stuck_conversion`` reads the raw
    # window for that); treating it as a guard would lock the manual recovery
    # doors on a RUNNING box for as long as the owner takes to finish signing
    # in, since every re-evaluation re-stamps the anchor.
    if container.get("conversion_deferred_reason"):
        return False
    return _within_guard_window(container.get("conversion_started_at"))


async def _stamp_conversion_dispatch(owner_id: str) -> None:
    """Claim the container row for a conversion. Awaited BEFORE every dispatch
    (webhook and self-heal alike) so the marker is durable even if the
    fire-and-forget task never gets to run, and so the rate-limit anchor is
    already on the row when the next ~3s poll reads it.

    ``update_conversion_fields``, whose ALLOWLIST is the point: every
    conversion-marker write can race a concurrent provision, and pinning
    this to the marker fields means it can never reach ``status`` —
    see that helper's docstring for the live incident this closed."""
    await container_repo.update_conversion_fields(
        owner_id,
        {
            "conversion_pending": True,
            "substatus": CONVERTING_SUBSTATUS,
            "conversion_started_at": utc_now_iso(),
            # A fresh dispatch is NOT deferred until run_conversion_reprovision
            # decides it is. Clearing here is load-bearing: a stale marker left
            # from an earlier deferral would read as "never mid-drain" and
            # disarm the guard during THIS dispatch's real stop + drain.
            "conversion_deferred_reason": None,
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
    (DDB blip, misconfig), running the conversion — every provision step is
    idempotent, so a duplicate converges — beats silently stranding a
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


"""Strong refs for fire-and-forget provisioning tasks — see
``dispatch_metronome_provisioning``. Module-level because the tasks outlive
the request that spawned them."""
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _provision_metronome_customer(owner_id: str) -> None:
    """Best-effort Metronome customer provisioning. Never raises."""
    from core.billing.metronome_provisioning import ensure_metronome_customer

    try:
        await ensure_metronome_customer(owner_id=owner_id)
    except Exception:
        put_metric("metronome.provision.error")
        logger.exception(
            "Metronome customer provisioning failed for owner %s on entering_live — "
            "the credits gate fails closed and the top-up path re-ensures",
            owner_id,
        )


def dispatch_metronome_provisioning(owner_id: str) -> None:
    """Fire the Metronome provisioning in the background.

    Its own function rather than an inline ``create_task`` for two reasons: it
    is the seam tests patch to isolate the webhook's other dispatches, and the
    fire-and-forget is a deliberate choice worth naming. Provisioning is
    several Metronome round trips; awaiting them inside a Stripe webhook risks
    a timeout, and a retried webhook re-enters with the status ALREADY flipped
    off ``trial_local`` — which silently skips the transition branches
    entirely (the same hazard the conversion branch documents).

    ``is_owner_blocked`` fails CLOSED for a bedrock_claude owner with no
    customer, so a provisioning failure blocks spend rather than silently
    spending our Bedrock budget.
    """
    # Hold a strong reference until the task finishes. asyncio keeps only a
    # WEAK reference to a running task, so a bare fire-and-forget
    # `create_task(...)` whose result nobody stores can be garbage-collected
    # mid-await — the documented footgun in the asyncio docs. That would drop
    # provisioning silently: `_provision_metronome_customer`'s except clause
    # only catches failures the coroutine actually reaches, so a collected
    # task emits no metric and no log, and nothing re-attempts it for a
    # personal owner.
    task = asyncio.create_task(_provision_metronome_customer(owner_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _request_company_stripe(owner_id: str) -> None:
    """Best-effort "connect the company's Stripe account" card. Never raises."""
    from core.services import company_stripe_approval

    try:
        await company_stripe_approval.request_company_stripe(
            owner_id=owner_id,
            issue_id=None,
            reason=(
                "Your subscription is live. Connecting the company's Stripe account "
                "turns on payments, customers and revenue reporting."
            ),
        )
    except Exception:
        put_metric("company_stripe.request.error")
        logger.exception(
            "Could not file the company Stripe request for owner %s on entering_live — "
            "the owner can still connect from Settings, and Friday can re-file",
            owner_id,
        )


def dispatch_company_stripe_request(owner_id: str) -> None:
    """Ask the owner to set up (or connect) the company's Stripe account.

    Nothing platform-side asked before this: ``request_company_stripe`` had a
    single caller, the container hook route, so the card appeared only when
    Friday spontaneously filed it — and the resulting
    ``company_stripe_account_id`` is what the whole Money data plane
    (Payments / Customers / Products / Home rollups) reads through.

    We ASK, never auto-create. Creating the account for the owner would
    foreclose "connect my existing Stripe" (``connect/start`` 409s once bound),
    permanently stamp a raw owner_id as the Stripe display name, and flip owner
    facts to ``pending`` — which the fleet skill reads as "already requested,
    do not ask again".

    Idempotent twice over, so firing it here is safe: the call short-circuits
    when an account is already bound, and ``file_board_card`` dedupes on the
    pending ``company_stripe`` marker, so at most one card exists at a time.

    Fire-and-forget for the same reason as ``dispatch_metronome_provisioning``
    — filing a card is several Paperclip round trips (company lookup, admin
    cookie, pending list, then the create), too many to await inside a Stripe
    webhook, and the strong reference below is what stops asyncio garbage-
    collecting the task mid-await.

    An owner whose Paperclip company does not exist yet gets NO card and no
    error: ``file_board_card`` returns None on the company gate. Reachable only
    on the rare direct-to-paid entry (no prior ``trial_local`` row, so no
    provisioned container yet), and ``entering_live`` is a one-shot edge that
    never re-fires — the owner's Settings page and Friday remain the fallbacks,
    exactly as they were before this dispatch existed.
    """
    task = asyncio.create_task(_request_company_stripe(owner_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def start_trial_conversion(owner_id: str, flight_key: str) -> bool:
    """Begin the trial→paid conversion for one owner, at most once.

    The claim/stamp/dispatch/rollback dance below is provider-AGNOSTIC despite
    living in a Stripe-named module — it turns the locked-down trial container
    into a paid one, which has nothing to do with who took the money. Apple IAP
    needs exactly the same thing on its own purchase path, so this is the seam
    rather than a second copy of the sequence.

    ``flight_key`` single-flights the entry: the Stripe path passes the
    subscription id (several events arrive per change, unordered), the Apple
    path the originalTransactionId (a link call and a SUBSCRIBED notification
    describe the same purchase). Returns True iff this caller ran it.
    """
    if not await _claim_conversion_entry(owner_id, flight_key):
        return False
    try:
        await _stamp_conversion_dispatch(owner_id)
        asyncio.create_task(run_conversion_reprovision(owner_id))
        return True
    except Exception:
        logger.exception("Trial-conversion dispatch failed for owner %s", owner_id)
        # Free the claim so a sibling event or redelivery can retry a
        # conversion that never got as far as the stamp. If the stamp DID land,
        # the conversion_pending self-heal is the recovery path either way.
        await _rollback_conversion_entry(owner_id, flight_key)
        return False


async def _conversion_prerequisite_gate(owner_id: str) -> str | None:
    """The prerequisite gate code that would make the provisioner fail, or
    None when the reprovision can proceed. Fails OPEN on a gate-read error: a
    DDB blip must not strand a paying owner on the trial shape — the
    provisioner is the real fence and its failure is the recoverable kind."""
    from core.services import provision_gate

    try:
        # probe_, not evaluate_: this is an internal check that runs on every
        # conversion dispatch and every self-heal re-evaluation while deferred;
        # the metric-emitting variant would inflate the provision.gate.blocked
        # stream behind the spike alarm for a routine, separately-metered state
        # (billing.trial_conversion.reprovision{outcome=deferred}).
        gate = await provision_gate.probe_provision_gate(owner_id=owner_id)
    except Exception:  # noqa: BLE001 — fail open, see docstring
        logger.exception("Conversion prerequisite check failed for owner %s — proceeding to reprovision", owner_id)
        return None
    if gate is not None and gate.code in _CONVERSION_PREREQUISITE_GATES:
        return gate.code
    return None


async def rearm_deferred_conversion(owner_id: str) -> bool:
    """Completion hook for a conversion prerequisite (the ChatGPT device-code
    flow finishing): if this owner's conversion is pending AND deferred, drop
    the self-heal's rate-limit anchor and the deferral marker so the very next
    ``/container/status`` poll re-fires the reprovision, instead of waiting
    out the guard window. Returns whether it re-armed anything.

    Leaves a non-deferred pending conversion alone: its guard stamp protects a
    drain that may be in flight, and clearing it would let the poll's
    auto-retry put a second task on the EFS WAL. Best-effort by contract — the
    self-heal re-evaluates every guard window regardless.

    The clear is a CAS on the deferral this call observed
    (``container_repo.clear_conversion_deferral``): a fresh dispatch clears the
    marker on its way in, so a re-arm racing it — or one whose eventually-
    consistent read returned the pre-stamp row — refuses instead of disarming
    the guard of a dispatch that may already be draining.
    """
    container = await container_repo.get_by_owner_id(owner_id)
    reason = (container or {}).get("conversion_deferred_reason")
    if not container or not container.get("conversion_pending") or not reason:
        return False
    if not await container_repo.clear_conversion_deferral(owner_id, deferred_reason=reason):
        return False  # a fresh dispatch won the row in between — its guard stays
    put_metric("billing.trial_conversion.rearmed")
    logger.info("Re-armed the deferred trial-conversion reprovision for owner %s", owner_id)
    return True


async def run_conversion_reprovision(owner_id: str) -> None:
    """Reprovision a converted owner's container off the trial shape, then
    release the row. asyncio.create_task target — never raises.

    Deferral: when a prerequisite the PROVISIONER needs is still missing
    (``_CONVERSION_PREREQUISITE_GATES`` — the owner chose ChatGPT at checkout
    and has not finished the device-code flow yet), the container is NOT
    stopped. Stopping it and dispatching would only produce a failed provision
    (``No ChatGPT OAuth tokens …``) and an owner staring at "Setup failed"
    while they are mid sign-in. The trial box keeps serving; the row keeps
    ``conversion_pending`` (the record that this paying owner is still on the
    trial shape) AND ``conversion_started_at`` (the self-heal's rate-limit
    anchor, so it re-evaluates every guard window), and gains
    ``conversion_deferred_reason`` so the prerequisite's completion path can
    re-arm it at once (``rearm_deferred_conversion``).

    Release semantics, both paths:
      - success: substatus cleared AND `conversion_pending` dropped.
        ``reconcile_runtime`` has re-derived the owner's AIPs and rewritten
        every agent profile from the now-paid provider_choice (or, with no
        live runtime yet, dispatched the ordinary provision funnel). Nothing is stopped or drained first —
        AgentCore has no task to scale to zero, and the profile rewrite the
        provision performs is what the next turn reads.
      - failure: substatus cleared — never strand the row in a state nothing
        acts on — but `conversion_pending` DELIBERATELY survives. It is the
        only record that this owner is paying for a trial-shaped container,
        and it is what `maybe_resume_stuck_conversion` re-fires off.
    """
    missing = await _conversion_prerequisite_gate(owner_id)
    if missing:
        put_metric("billing.trial_conversion.reprovision", dimensions={"outcome": "deferred"})
        logger.info(
            "Trial-conversion reprovision for owner %s deferred (%s): container left running on the "
            "trial shape until the owner completes it",
            owner_id,
            missing,
        )
        try:
            await container_repo.update_conversion_fields(
                owner_id, {"substatus": None, "conversion_deferred_reason": missing}
            )
        except Exception:
            logger.exception("Failed to record the deferred conversion for owner %s", owner_id)
            return
        # Close the re-arm race: a completion that landed between the probe
        # above and the write just made found no deferral marker to clear and
        # no-op'd. Re-probe now that the marker is durable; if the prerequisite
        # is already met, re-arm ourselves so the next poll fires instead of
        # waiting out the window.
        try:
            if await _conversion_prerequisite_gate(owner_id) is None:
                await rearm_deferred_conversion(owner_id)
        except Exception:
            logger.exception("Post-deferral re-check failed for owner %s — the self-heal will retry", owner_id)
        return

    succeeded = False
    try:
        from core.services.hermes_provisioning import reconcile_runtime

        succeeded = await reconcile_runtime(owner_id) != "failed"
    except Exception:
        logger.exception("Trial-conversion reprovision failed for owner %s", owner_id)
    put_metric(
        "billing.trial_conversion.reprovision",
        dimensions={"outcome": "ok" if succeeded else "error"},
    )

    # ALLOWLISTED release (``update_conversion_fields``) — pinned to the marker
    # fields, so it can never reach status. The old full-item put here
    # resurrected status="stopped" over the dispatch's fresh "provisioning"
    # (the #1428 dev incident).
    # A real attempt was made either way: the deferral marker is spent.
    fields: dict[str, Any] = {"substatus": None, "conversion_deferred_reason": None}
    if succeeded:
        fields["conversion_pending"] = False
        # Clear the TIMESTAMP too, not just the flag. ``conversion_guard_active``
        # is defined as "the TIMESTAMP is the guard, not the substatus", so
        # leaving it behind keeps every door the guard covers shut for the
        # remainder of the 300s window even though the conversion is finished —
        # a conversion that completes in 90s used to disarm nothing for another
        # 210s. That was survivable while the guard only covered the
        # stopped/error branch of /recover; now that it is hoisted above the
        # status split it also owns the MANUAL "Restart Gateway" action, so a
        # stale stamp means the user clicks Restart on a genuinely wedged
        # gateway and gets `already_recovering` for minutes. Clearing on
        # success costs nothing: the failure path deliberately keeps both
        # markers so `maybe_resume_stuck_conversion` can still self-heal.
        fields["conversion_started_at"] = None
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

    # Two different questions off the same stamp: ``window`` is the self-heal's
    # rate limit (a dispatch happened recently — in flight OR deferred — so do
    # not fire another); ``guard`` is whether a drain may be in flight, which a
    # deferred row never is (see conversion_guard_active).
    window = _within_guard_window(container.get("conversion_started_at"))
    guard = conversion_guard_active(container)
    if not container.get("conversion_pending"):
        # No pending conversion. `guard` is still honoured: it covers the brief
        # window between a successful hand-off (which drops the flag) and
        # dispatch_provision moving the row off "stopped".
        #
        # EXCEPT for the stranded shape: stopped with NO recorded error. A
        # healthy hand-off moves the row to "provisioning" (dispatch writes it);
        # a row still "stopped" has nothing running, and honouring the guard
        # here would stand the poll's wake path down for the full window on a
        # row nobody owns. Release the guard instead: the caller's wake branch
        # re-checks every gate (paid, trial, parked) and its dispatch no-ops
        # while an execution is RUNNING. The drain window itself is NOT
        # exposed — conversion_pending is still True there, which keeps the
        # branch above this one.
        if guard and container.get("status") == "stopped" and not container.get("last_error"):
            put_metric("billing.trial_conversion.stranded_row")
            logger.warning(
                "Conversion left owner %s stopped with no error — "
                "releasing the conversion guard so the status poll can wake the row",
                owner_id,
            )
            return False
        return guard
    if window:
        # In flight, re-dispatched, or DEFERRED within the window. Hands off —
        # this is the rate limit that keeps the ~3s poll from storming
        # reprovisions. A deferred row reports False: its container is running
        # and nothing is draining, so the caller may auto-retry / recover it
        # like any other row. It re-evaluates when the window lapses, or at
        # once when the prerequisite's completion path re-arms it
        # (``rearm_deferred_conversion`` drops the anchor).
        return guard

    # Pending + the window lapsed == the dispatch died. Stripe will never
    # re-fire it. Re-dispatch, but only for an owner who is actually paying.
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        status = (account or {}).get("subscription_status")
        if status not in _PAID_SUBSCRIPTION_STATUSES:
            # Not (or no longer) paid — e.g. the subscription was canceled
            # before the conversion landed. Drop the flag and let the ordinary
            # auto-retry + provision gate handle the row. Scoped write, like
            # every other marker write here.
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


def _workspace_plan_from_subscription_items(event_data: dict[str, Any]) -> str | None:
    """Return the configured Workspace/Ultra plan for one recurring item only.

    A workspace subscription has exactly one recurring Stripe item.  Never
    persist a subscription event whose item shape cannot prove that invariant:
    accepting an unknown or multi-item payload can mask an unsupported
    recurring charge.
    """
    items = (event_data.get("items") or {}).get("data") or []
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
    new_plan = _workspace_plan_from_subscription_items(event_data)
    if new_plan is None:
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

    if entering_live:
        # Metronome customer lifecycle. THIS transition is what makes the owner
        # Metronome-eligible: ``ensure_metronome_customer`` deliberately
        # short-circuits for ``trial_local`` (free-tier owners draw no prepaid
        # credits — see its docstring), so a converting owner only becomes
        # eligible the moment the status flips here.
        #
        # It cannot be left to the status poll. Its only other call site is
        # ``ensure_user_ready``, and ``routers/container.py`` re-dispatches that
        # on a RUNNING container solely for org-context callers or a failed
        # Teams seed — a healthy PERSONAL owner never re-enters it. A trial→paid
        # conversion always has a running container, so before this call no
        # converted personal owner ever got a Metronome customer, contract, or
        # low-balance alerts. That left ``metronome_gate.is_owner_blocked``
        # failing open forever (no customer => not blocked) and the owner
        # running uncredited Bedrock — Opus-tier for Friday — on our account.
        # Verified on prod 2026-08-06: the only owner carrying a
        # metronome_customer_id was the org (org context always re-dispatches).
        #
        # Only Bedrock-managed owners draw prepaid credits. Provisioning a
        # ChatGPT-OAuth or BYO-key owner would mint a Metronome customer, a
        # contract and two low-balance alerts for inference we never bill, and
        # those alerts feed the fast-gate webhook — filling the ledger with
        # zero-balance customers the enforcement path then has to keep ignoring.
        #
        # Skip only on a POSITIVE non-Bedrock signal, never on absence: a
        # provider we failed to resolve is provisioned anyway, because
        # under-provisioning re-opens the uncredited-spend hole this whole
        # change exists to close, while over-provisioning only wastes a few
        # API calls. Same resolution the trial_converted telemetry uses —
        # metadata is only trusted when its row write actually committed.
        resolved_provider = (metadata_provider if provider_persisted else None) or account.get("provider_choice")
        if resolved_provider not in ("chatgpt_oauth", "byo_key"):
            dispatch_metronome_provisioning(account["owner_id"])

        # Launch Competition: this first-activation transition is what counts
        # a referred owner toward their referrer (and retries the owner's own
        # pending milestone credit grants). Best-effort — never raises; its
        # own CAS dedup makes duplicate/unordered deliveries inert.
        # NOTE: the Apple IAP activation path does NOT come through here — it
        # must call on_subscription_activated too when the mobile app ships.
        from core.services import launch_referral

        await launch_referral.on_subscription_activated(account["owner_id"])

        # Ask the owner to connect the company's Stripe account. Unconditional
        # on the LLM provider above: this is the owner's own revenue rail, not
        # ours, and every paid shape needs it for the Money surfaces to hold
        # anything at all.
        dispatch_company_stripe_request(account["owner_id"])

        # Listening is paid, and the #general seed runs once — so a room seeded
        # on the trial keeps its CEO on Mentions after upgrading. First entry
        # into paid only: a canceled owner was paid before, so a Mentions CEO
        # may be their own choice, and reconciliation never resets a user's
        # choice. Best-effort: the status already committed, so a raise here
        # would 500 the webhook and Stripe's retry would never re-enter this.
        if old_status != "canceled":
            from core.services import messaging

            try:
                await messaging.promote_default_room_ceo(account["owner_id"])
            except Exception:
                logger.exception("CEO listening promotion failed for owner %s", account["owner_id"])

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
    if old_status == "trial_local" and new_status in ("trialing", "active"):
        # Trial→paid conversion: the workspace is still on the locked-down trial shape (its
        # single sonnet AIP, the trial provider block in every agent profile). A provision
        # re-derives the owner's AIPs and rewrites every profile from the now-paid
        # provider_choice, which the status persist above already committed — so by the time
        # this runs, resolve_provider_choice reads the paid value. Nothing is stopped or
        # drained: there is no task to scale to zero, and the agent reads the rewritten
        # profile on its next turn.
        # Best-effort: the status persist above already committed, so a raise
        # here would 500 the webhook and Stripe's retry would see
        # old_status != "trial_local" and never re-fire this branch — log and
        # continue instead of dropping the reprovision behind a 500.
        #
        # The stamp is AWAITED here, inside the webhook, before the dispatch:
        #   - it must land before the provision, or the owner's ~3s /container/status poll
        #     can dispatch a second one underneath it;
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
    elif provider_persisted or (plan_persisted and account.get("plan") != new_plan):
        # A committed provider or plan change outside a conversion (which reconciles in
        # run_conversion_reprovision): the plan decides the CEO's premium tier and, for Base,
        # the provider itself. Background -- reconcile_runtime never raises.
        from core.services.hermes_provisioning import reconcile_in_background

        reconcile_in_background(account["owner_id"])

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


async def handle_checkout_session(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    """Handle the Checkout events still owned outside subscription lifecycle.

    Subscription state is handled exclusively by ``customer.subscription.*``.
    Customer credit purchases use Metronome payment-gated commits directly;
    there is no Stripe Checkout credit-settlement or bundled-credit branch.
    """
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
    "checkout.session.completed": handle_checkout_session,
    "checkout.session.async_payment_succeeded": handle_checkout_session,
}
