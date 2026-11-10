"""Provision gate evaluation — single source of truth for whether an owner
can provision a container, and *why not* when they can't.

Both `/container/provision` and `/container/status` consult this helper and
return its structured payload on 402, so the two endpoints can never
disagree about the gate state.

This module also owns the pure billing-state predicates that
``evaluate_provision_gate`` (and several callers outside the gate) need:
``is_subscription_active``, ``is_paid_subscription``, ``is_trial_blocked``,
and ``has_always_on_entitlement``. They live here so every site that asks
"is this billing account currently usable?" (or "is it paid?") hits
the same code path — see the call sites in ``core.services.chat_gate``,
``routers.config`` and ``routers.billing``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.auth import WORKSPACE_REQUIRED_CODE, WORKSPACE_REQUIRED_MESSAGE, is_org_owner_id
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_once_per_owner_per_day
from core.repositories import billing_repo
from core.services import trial_cap

logger = logging.getLogger(__name__)


# Stripe statuses that grant platform use right now. "trialing" is legacy:
# checkout no longer creates Stripe trials (billed immediately since
# 2026-07-17), but in-flight trialing rows from before the cutover remain
# valid until they convert or cancel.
# "trial_local" is the no-card local trial — a Stripe-less state, but it
# grants platform use exactly like "active" does.
_ACTIVE_STATUSES = frozenset({"active", "trialing", "trial_local"})


def _paid_voice(account: Mapping[str, Any] | None) -> bool:
    # Runtime import: voice_billing also uses trial_cap. The invoice-derived entitlement
    # remains independent of the workspace's Stripe/Apple subscription fields.
    from core.billing.voice_billing import has_voice_plan

    return has_voice_plan(dict(account)) if account else False


# Stripe statuses that indicate this account has *ever* been on a paid
# subscription (or is currently mid-attempt). Used to block a second
# POST /trial-checkout — Stripe Checkout in subscription mode creates a
# NEW subscription on every successful return, so an unguarded retry
# double-subscribes (and, now that checkout bills immediately,
# double-charges). A user who legitimately wants to re-subscribe after
# cancel must go through customer support.
#
# Exported so the legacy-row recovery path in ``routers.billing`` can
# apply the same set against a *live Stripe Subscription object* (not
# the local billing row). ``is_trial_blocked`` is the right call when
# you have a billing-account row; for a raw Stripe status string, test
# against this constant directly.
TRIAL_BLOCKED_STATUSES = frozenset(
    {
        "active",
        "trialing",
        "past_due",
        "canceled",
        "incomplete",
        "incomplete_expired",
        "unpaid",
        "paused",
    }
)

# The subset of the above that can still bill going forward. A subscription in
# one of these states existing on the Stripe CUSTOMER (found by live list, not
# the local row) means a new Checkout would double-subscribe the same owner.
# Terminal states (canceled, incomplete_expired) are excluded: they can never
# bill again, and whether a canceled owner may re-subscribe is a local-row
# policy question (`is_trial_blocked`), not a double-charge risk.
LIVE_SUBSCRIPTION_STATUSES = TRIAL_BLOCKED_STATUSES - {"canceled", "incomplete_expired"}


def is_subscription_active(account: Mapping[str, Any] | None) -> bool:
    """Whether this billing-account row currently allows platform use.

    Returns True when:
      - the voice plan has collected, current invoice coverage, OR
      - ``subscription_status`` is in {"active", "trialing"}, OR
      - ``subscription_status`` is None *and* ``stripe_subscription_id`` is
        set — legacy fallback for rows whose status hasn't been backfilled
        by ``customer.subscription.updated`` yet. Without this, paid users
        get pushed back into the payment phase.

    Otherwise False, including for ``None`` accounts (no billing row =
    not subscribed).

    The legacy fallback is intentionally guarded on ``status is None`` —
    a *non-None* status that isn't in the active set (e.g. "canceled",
    "past_due") must be treated as not-subscribed regardless of whether
    ``stripe_subscription_id`` is still on the row. Our cancel path
    (``billing_service.cancel_subscription``) clears
    ``stripe_subscription_id`` so the field-stale case shouldn't happen
    in practice, but pinning the strict semantic here prevents any
    future code change from silently re-introducing the divergence.
    """
    if not account:
        return False
    if _paid_voice(account):
        return True
    status = account.get("subscription_status")
    if status in _ACTIVE_STATUSES:
        return True
    return status is None and bool(account.get("stripe_subscription_id"))


# Statuses that carry a PAID subscription. Deliberately excludes
# "trial_local" — the no-card local trial grants platform use (see
# ``_ACTIVE_STATUSES``) but is not a subscription.
_ENTITLED_STATUSES = frozenset({"active", "trialing"})


def is_paid_subscription(account: Mapping[str, Any] | None) -> bool:
    """PAID predicate — a collected, currently covered voice plan or a real
    (or legacy-implied) Workspace subscription.

    Distinct axis from ``is_subscription_active`` (platform use):
    ``trial_local`` owners ARE subscription-active — chat and interactive
    Teams are part of the free tier — but they have NOT subscribed. The
    /billing/account ``is_subscribed`` wire field keys off this predicate
    so billing surfaces render the free tier as "not subscribed" (Subscribe
    CTA), never as "Active subscription". Fail-closed: no row = not paid.

    The legacy fallback mirrors ``is_subscription_active`` exactly:
    it only applies while ``subscription_status`` is None (rows whose
    status hasn't been backfilled yet), so an explicit non-paid status —
    ``trial_local``, ``canceled``, ``past_due`` — can never ride a stale
    ``stripe_subscription_id`` into paid status.
    """
    if not account:
        return False
    if _paid_voice(account):
        return True
    status = account.get("subscription_status")
    if status in _ENTITLED_STATUSES:
        return True
    return status is None and bool(account.get("stripe_subscription_id"))


def has_always_on_entitlement(account: Mapping[str, Any] | None) -> bool:
    """Paid-only predicate for ALWAYS-ON features (webhooks, channels,
    routines, autonomous heartbeats).

    Autonomy follows the paid predicate. An unchanged ``trial_local`` row can
    also carry a separately collected voice plan; its invoice-derived coverage
    grants the same runtime entitlement without rewriting provider billing fields.
    Kept as a distinct name because entitlement-to-autonomy and
    rendered-billing-state are different questions that merely share an
    answer today — call sites should keep saying which one they mean.
    """
    return is_paid_subscription(account)


def is_trial_blocked(account: Mapping[str, Any] | None) -> bool:
    """Whether this account has a subscription state that should block
    creating a fresh subscription checkout. See ``TRIAL_BLOCKED_STATUSES``.

    Returns False for missing accounts (a brand-new owner with no row
    is the canonical "checkout-allowed" state).
    """
    if not account:
        return False
    # A pending voice invoice can still collect. A second Workspace checkout
    # must not add an unrelated subscription while that enrollment is in flight.
    if account.get("metronome_voice_contract_id"):
        return True
    return account.get("subscription_status") in TRIAL_BLOCKED_STATUSES


@dataclass(frozen=True)
class Gate:
    """A blocked-state gate result. None means no gate fires (provision allowed)."""

    code: str
    title: str
    message: str
    action_label: str
    action_href: str
    action_admin_only: bool
    owner_role: str  # "admin" | "member"

    def to_payload(self) -> dict[str, Any]:
        """Build the FastAPI HTTPException detail payload.

        ``detail`` is kept as a plain string for backwards-compat with any
        path that still renders FastAPI's default error shape. ``blocked``
        is the structured field new frontends switch on.
        """
        return {
            "detail": self.message,
            "blocked": {
                "code": self.code,
                "title": self.title,
                "message": self.message,
                "action": {
                    "kind": "link",
                    "label": self.action_label,
                    "href": self.action_href,
                    "admin_only": self.action_admin_only,
                },
                "owner_role": self.owner_role,
            },
        }


def _team_billing_required_gate(owner_role: str) -> Gate:
    """The workspace subscription paywall gate.

    An org owner without an active workspace subscription is always routed
    here. The stable machine code keeps existing clients compatible while the
    user-facing wording describes the seat-free contract.
    """
    return Gate(
        code="team_billing_required",
        title="Set up workspace billing",
        message="Your workspace needs an active subscription to start.",
        action_label="Set up billing",
        action_href="/settings/billing",
        action_admin_only=True,
        owner_role=owner_role,
    )


async def _has_oauth_tokens(token_owner: str, owner_id: str) -> bool:
    """Whether ``token_owner`` has ChatGPT OAuth tokens bound to ``owner_id``.

    OAuth tokens live in the ``oauth-tokens`` DynamoDB table keyed by Clerk
    user_id (see ``core.services.oauth_service``). Callers pass the resolved
    token owner — the *connector* (the workspace owner who completed the
    device-code flow, ``connector_from_account``), not the polling user.

    ``owner_id`` is the workspace whose container consumes the tokens; we pass
    it as ``bound_to`` so a row the connector claimed for a *different*
    workspace reads as no-tokens — the gate then fires ``oauth_required``
    instead of silently passing on a row that belongs to another container.

    Lazy import to avoid pulling the OAuth subsystem at module import time.
    """
    from core.services import oauth_service

    return await oauth_service.has_active_oauth(user_id=token_owner, bound_to=owner_id)


def trial_daily_cap_gate(owner_role: str) -> Gate:
    """The blocked-gate a trial_local owner sees once they cross the $3/day
    $3/day trial cap. Shared so the provision gate and the /container/status wake
    path render an identical 'daily limit' card (not a spinner)."""
    return Gate(
        code="trial_daily_cap",
        title="Daily free limit reached",
        message="You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
        action_label="Upgrade",
        action_href="/settings?panel=billing",
        action_admin_only=False,
        owner_role=owner_role,
    )


def gate_for_trial_reason(reason: str | None, owner_role: str) -> Gate | None:
    """Map a ``trial_cap.trial_block_reason`` result to the card it renders.

    The ONE reason->Gate mapping. Every door that can start, wake, or dispatch
    work on a trial container — the provision gate below, ``/container/recover``,
    and the Teams BFF run guards — routes through this, so they cannot render
    different verdicts (or different copy) for the same owner. Two of those
    doors previously inlined only the daily-cap half of the predicate and would
    happily wake an EXPIRED trial; that is the bug this exists to make
    unrepresentable. Returns None when the owner is not blocked.
    """
    if reason == trial_cap.TRIAL_EXPIRED:
        return trial_expired_gate(owner_role)
    if reason == trial_cap.DAILY_CAP:
        return trial_daily_cap_gate(owner_role)
    if reason == trial_cap.INCLUDED_CAP:
        return included_daily_cap_gate(owner_role)
    return None


def included_daily_cap_gate(owner_role: str) -> Gate:
    """A PAYING capped owner (Base, or Workspace/Ultra on the interim managed default) at the
    daily included limit. Never the trial card: they are paying. Final copy is keyed on this
    code by the frontend; this text is the neutral fallback."""
    return Gate(
        code="included_daily_cap",
        title="Today's included usage is used up",
        message="You've used today's included usage. It resets at 00:00 UTC.",
        action_label="Billing",
        action_href="/settings?panel=billing",
        action_admin_only=False,
        owner_role=owner_role,
    )


def trial_expired_gate(owner_role: str) -> Gate:
    """The blocked-gate a trial_local owner sees once their 30-day window
    (``trial_end``, armed by the first canonical ready status poll) has
    passed. Mirrors ``trial_daily_cap_gate`` field-for-field
    so the provision gate and gate_chat's Layer 2 render an identical
    'trial ended' card instead of a spinner or a generic error."""
    return Gate(
        code="trial_expired",
        title="This workspace is reading-only",
        message="Reading-only — agents start again when the plan is on.",
        action_label="See the plan",
        action_href="/settings?panel=billing",
        action_admin_only=False,
        owner_role=owner_role,
    )


async def evaluate_provision_gate(
    *,
    owner_id: str,
    is_admin: bool = True,  # server-side callers (webhooks, wake paths) read as the workspace admin
    actor_user_id: str | None = None,  # the human polling, when there is one: the PostHog person
) -> Gate | None:
    """Evaluate the provision gate, emitting a metric on a blocked decision.

    Thin wrapper over ``_evaluate_provision_gate_inner`` that owns the single,
    all-branches-covered metric emission. A non-None gate (provisioning blocked)
    emits ONE ``provision.gate.blocked{code}`` — ``put_metric`` publishes the
    {env,service} rollup alongside it, which is the stream the W64 spike alarm
    reads. This used to be TWO calls, the second dimensionless, because the
    emitter published a single dimension set and a coded emission was therefore
    invisible to a {env,service} query. Re-adding that call would DOUBLE-COUNT
    every block on the rollup and halve W64's effective threshold.
    """
    gate = await _evaluate_provision_gate_inner(owner_id=owner_id, is_admin=is_admin)
    if gate is not None:
        # Bounded ``code`` set → safe metric dimension. Best-effort: a metrics
        # outage must never block the gate decision (which is the request path).
        try:
            put_metric("provision.gate.blocked", dimensions={"code": gate.code})
        except Exception:  # noqa: BLE001 — telemetry must not break provisioning
            logger.debug("provision.gate.blocked metric emission failed", exc_info=True)
        if gate.code == "trial_expired":
            # Trial funnel telemetry (customer-analytics twin of the EMF
            # metric above): /container/status polls every ~3s while
            # blocked, so dedup to once per owner per UTC day.
            capture_once_per_owner_per_day(owner_id=owner_id, event="trial_expired_block", distinct_id=actor_user_id)
    return gate


async def probe_provision_gate(*, owner_id: str, is_admin: bool = True) -> Gate | None:
    """Metric-free gate evaluation for diagnostic reads (the admin surface).

    ``evaluate_provision_gate``'s blocked-metric stream feeds the W64
    spike alarm; an operator inspecting an owner must not inflate it.
    Same decision logic, no emission.

    Metric-free is not the same as side-effect-free: for an ``openrouter_managed``
    owner this still calls ``metronome_gate.is_owner_blocked``, and a stale
    fast-gate row there triggers the same lazy ``getNetBalance`` refresh (cache
    rewrite + enforcement re-assert + pull-based Bedrock restore) that any
    other owner poll would — an admin read can self-heal an owner's gate state
    as a side effect of merely looking at it.
    """
    return await _evaluate_provision_gate_inner(owner_id=owner_id, is_admin=is_admin)


async def _evaluate_provision_gate_inner(
    *,
    owner_id: str,
    is_admin: bool = True,
) -> Gate | None:
    """Return a Gate if provisioning should be blocked, else None.

    Layers (matches existing _assert_provision_allowed logic):
    0. The owner must be a workspace (Clerk org).
    1. Subscription must be active or trialing (or legacy stripe_subscription_id present).
    2. For openrouter_managed: credit balance must be above the reserve floor.
    3. For chatgpt_oauth: OAuth tokens must exist for the connector — the
       owner who completed the device-code flow (the polling user is
       irrelevant).
    """
    owner_role = "admin" if is_admin else "member"

    # Layer 0 — workspace. Personal workspaces no longer exist (the September
    # 2026 cohort was purged or migrated), so a non-org owner has nothing to
    # provision. Product APIs 409 such a caller in resolve_owner_id before they
    # get here; this is what a server-side door (wake, webhook, admin probe)
    # handed a stale user_ id sees.
    if not is_org_owner_id(owner_id):
        return Gate(
            code=WORKSPACE_REQUIRED_CODE,
            title="Choose a workspace",
            message=WORKSPACE_REQUIRED_MESSAGE,
            action_label="Choose workspace",
            action_href="/onboarding",
            action_admin_only=False,
            owner_role=owner_role,
        )

    # Layer 1 — subscription.
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account:
        # No billing row at all: the workspace's trial row was never written
        # (ensure_workspace_trial_default creates it before the gate). Distinct
        # from an inactive subscription below, which has its own re-activation
        # path.
        return _team_billing_required_gate(owner_role)
    if not is_subscription_active(account):
        status = account.get("subscription_status")
        if status == "past_due":
            return Gate(
                code="payment_past_due",
                title="Payment past due",
                message="Your latest invoice failed. Update your payment method to continue.",
                action_label="Update payment",
                action_href="/settings/billing",
                action_admin_only=True,
                owner_role=owner_role,
            )
        # Align routing with what /billing/trial-checkout will actually
        # accept. trial-checkout 409s any row whose subscription_status
        # is in TRIAL_BLOCKED_STATUSES
        # (canceled, incomplete, incomplete_expired, unpaid, paused —
        # plus active/trialing, but those are filtered out by
        # is_subscription_active above). Those rows must be routed to
        # /settings/billing — sending them to the picker would 409
        # already_subscribed regardless of whether stripe_subscription_id
        # is still set (cancel_subscription clears it but leaves
        # status="canceled").
        if is_trial_blocked(account):
            return Gate(
                code="subscription_required",
                title="Subscription not active",
                message="Reactivate your subscription to start your container.",
                action_label="Manage subscription",
                action_href="/settings/billing",
                action_admin_only=True,
                owner_role=owner_role,
            )
        # A workspace with a customer row but no subscription state (status
        # None) STARTED checkout and abandoned it — the customer row was
        # written by create_customer_for_owner before the Stripe tab. Route it
        # back to the workspace paywall; the checkout 409 guard excepts a
        # None-status row, so the retry from the paywall proceeds cleanly.
        return _team_billing_required_gate(owner_role)

    # trial_local short-circuit — a no-card local trial has NO
    # provider_choice on the row (its provider is a config-path mode resolved
    # at provision time, not stored here), so it must not be sent through the
    # Layer-2 provider_required check below. A trial_local owner is
    # provision-eligible without a stored provider.
    if account.get("subscription_status") == "trial_local" and not _paid_voice(account):
        # ONE predicate, shared with every start path and the
        # daily Bedrock-restore trigger, so the gate and those paths can
        # never disagree about whether a trial owner may have a container.
        # This branch used to inline its own expiry+cap check; the chokepoint
        # inlined only the cap, and an EXPIRED trial could therefore wake a
        # container the gate would have refused. Do not re-inline it.
        #
        # ``account`` is already in hand, so this spends no extra billing read.
        reason = await trial_cap.reason_for_account(owner_id, account)
        return gate_for_trial_reason(reason, owner_role)

    # Layer 2 — provider must be picked.
    #
    # Reads ``provider_choice`` directly off the billing row already in scope
    # so we don't burn a second DDB call for the same field, and so the gate
    # can never disagree with itself across two reads (a concurrent
    # ChatGPT-OAuth disconnect clears provider_choice — a re-read here used
    # to mask the null with the legacy "bedrock_claude" fallback).
    provider_choice = account.get("provider_choice")
    if provider_choice is None and (account.get("plan") in {"base", "standard", "ultra_pro"} or _paid_voice(account)):
        # Base, and (interim) a paid Workspace/Ultra row that never chose a
        # provider, run on the capped managed model (container_provision.
        # provider_choice_from_row). OpenRouter's per-key daily limit is the
        # valve, not Metronome credits, so Layer 3 has no jurisdiction here.
        return gate_for_trial_reason(await trial_cap.reason_for_account(owner_id, account), owner_role)
    if provider_choice is None:
        return Gate(
            code="provider_required",
            title="Pick how to power your agents",
            message=("Choose OpenRouter managed inference or sign in with ChatGPT to continue."),
            action_label="Pick provider",
            action_href="/settings?panel=billing",
            # Org members can't change the org's provider — only the admin can.
            action_admin_only=True,
            owner_role=owner_role,
        )

    # Layer 3 — provider-specific prerequisites.
    if provider_choice == "openrouter_managed":
        # Credits pool at the owner level — org members all draw from one
        # balance funded by the admin. Blocked-ness comes from the shared
        # Metronome fast-gate read (webhook-fed flag + lazy getNetBalance
        # refresh) so this gate and the pre-chat gate can never disagree.
        from core.services.metronome_gate import is_owner_blocked

        if await is_owner_blocked(owner_id):
            return Gate(
                code="credits_required",
                title="Top up Lightbulb credits to start your workspace",
                message="Top up Lightbulb credits to continue managed inference.",
                action_label="Top up now",
                # The PROVIDERS panel, not billing. Credits are sold by
                # CreditsSection, which LLMPanel embeds (see
                # settings-nav-config: "there is NO separate Credits row").
                # This pointed at /settings/billing#credits — but that route
                # only `router.replace`s to /settings?panel=billing, dropping
                # the fragment, and the billing panel has no credits UI at all.
                # The one CTA on the "you have no credits" card led somewhere
                # you cannot buy credits.
                action_href="/settings?panel=providers",
                action_admin_only=False,
                owner_role=owner_role,
            )

    if provider_choice == "chatgpt_oauth":
        # Gate on the CONNECTOR's tokens (the workspace owner who completed
        # the device-code flow), never the polling user — otherwise the gate
        # flaps per-poller in orgs: admin B would be told to "sign in" even
        # though owner A already connected. The billing row is already in
        # scope; connector_from_account costs no extra read on this poll path.
        from core.services.oauth_service import connector_from_account

        token_owner = connector_from_account(account, owner_id)
        if not await _has_oauth_tokens(token_owner, owner_id):
            return Gate(
                code="oauth_required",
                title="Sign in with ChatGPT",
                message="Complete the ChatGPT sign-in to start your container.",
                action_label="Sign in with ChatGPT",
                action_href="/chat?provider=chatgpt_oauth",
                # Only an admin (the owner) may bind a ChatGPT account to the
                # shared container.
                action_admin_only=True,
                owner_role=owner_role,
            )

    if provider_choice == "byo_key":
        # Existing workspaces may still carry a legacy BYO choice. They keep
        # running when their saved key is present; if the row is incomplete,
        # direct the admin to the provider settings so they can move to one of
        # the currently supported choices. New key setup is closed.
        if not account.get("byo_provider"):
            return Gate(
                code="byo_provider_required",
                title="Finish setting up your inference provider",
                message=(
                    "This workspace has an incomplete saved API-key setup. Choose OpenRouter managed "
                    "inference or sign in with ChatGPT to continue."
                ),
                action_label="Change provider",
                action_href="/settings?panel=billing",
                action_admin_only=True,
                owner_role=owner_role,
            )

    return None  # all gates pass — provisioning allowed
