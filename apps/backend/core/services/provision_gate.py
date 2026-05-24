"""Provision gate evaluation — single source of truth for whether an owner
can provision a container, and *why not* when they can't.

Both `/container/provision` and `/container/status` consult this helper and
return its structured payload on 402, so the two endpoints can never
disagree about the gate state.

This module also owns the two pure billing-state predicates that
``evaluate_provision_gate`` (and several callers outside the gate) need:
``is_subscription_active`` and ``is_trial_blocked``. They live here so
every site that asks "is this billing account currently usable?" hits
the same code path — see the call sites in ``core.gateway.connection_pool``,
``routers.config``, and ``routers.billing``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.repositories import billing_repo
from core.services import credit_ledger


# Stripe statuses that grant platform use right now. Trialing is included
# because the trial itself is a valid subscription state on Stripe — billing
# kicks in at trial_end without any state transition the caller can see.
_ACTIVE_STATUSES = frozenset({"active", "trialing"})

# Stripe statuses that indicate this account has *ever* been on a paid
# subscription (or is currently mid-attempt). Used to block fresh
# trial-creation so a churned customer can't loop POST /trial-checkout
# forever; Stripe charges us for ECS Fargate either way and the trial
# itself never bills. A user who legitimately wants to re-subscribe after
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


def is_subscription_active(account: Mapping[str, Any] | None) -> bool:
    """Whether this billing-account row currently allows platform use.

    Returns True when:
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
    status = account.get("subscription_status")
    if status in _ACTIVE_STATUSES:
        return True
    return status is None and bool(account.get("stripe_subscription_id"))


def is_trial_blocked(account: Mapping[str, Any] | None) -> bool:
    """Whether this account has a subscription state that should block
    creating a fresh 14-day trial. See ``TRIAL_BLOCKED_STATUSES``.

    Returns False for missing accounts (a brand-new owner with no row
    is the canonical "trial-allowed" state).
    """
    if not account:
        return False
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


async def _has_oauth_tokens(clerk_user_id: str) -> bool:
    """Whether the calling Clerk user has ChatGPT OAuth tokens on file.

    OAuth tokens live in the ``oauth-tokens`` DynamoDB table keyed by Clerk
    user_id (see ``core.services.oauth_service``). ChatGPT OAuth is a
    personal-only path (org owners use Bedrock), so the lookup is always
    by the calling user, not the owner_id.

    Lazy import to avoid pulling the OAuth subsystem at module import time.
    """
    from core.services import oauth_service

    tokens = await oauth_service.get_decrypted_tokens(user_id=clerk_user_id)
    return tokens is not None


async def evaluate_provision_gate(
    *,
    owner_id: str,
    clerk_user_id: str,
    is_admin: bool = True,  # personal owners are always admin of themselves
) -> Gate | None:
    """Return a Gate if provisioning should be blocked, else None.

    Layers (matches existing _assert_provision_allowed logic):
    1. Subscription must be active or trialing (or legacy stripe_subscription_id present).
    2. For bedrock_claude: credit balance must be > 0.
    3. For chatgpt_oauth: OAuth tokens must exist for the calling user.
    """
    owner_role = "admin" if is_admin else "member"

    # Layer 1 — subscription.
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account:
        # No billing row at all means the owner has never started signup.
        # This is *distinct* from an inactive subscription below, which
        # has its own re-activation path. Surfaced separately so the
        # /container/status endpoint can route admins to the
        # ProviderPicker (which calls POST /billing/trial-checkout —
        # the only way to create the first billing row) without
        # accidentally short-circuiting the canceled/unpaid/paused
        # cases that need /settings/billing instead.
        return Gate(
            code="signup_required",
            title="Subscribe to start your container",
            message="An active subscription is required to provision a container.",
            action_label="Subscribe",
            action_href="/onboarding",
            action_admin_only=True,
            owner_role=owner_role,
        )
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
        #
        # Conversely, when status is None — the row exists but Stripe
        # never assigned a state (e.g. /billing/trial-checkout wrote the
        # row, then Checkout-session creation failed or the user closed
        # the Stripe tab) — trial-checkout will proceed. Surface
        # signup_required so the frontend re-renders the picker for a
        # restart.
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
        return Gate(
            code="signup_required",
            title="Subscribe to start your container",
            message="An active subscription is required to provision a container.",
            action_label="Subscribe",
            action_href="/onboarding",
            action_admin_only=True,
            owner_role=owner_role,
        )

    # Layer 2 — provider must be picked.
    #
    # Reads ``provider_choice`` directly off the billing row already in scope
    # so we don't burn a second DDB call for the same field, and so the gate
    # can never disagree with itself across two reads (a concurrent
    # ChatGPT-OAuth disconnect clears provider_choice — a re-read here used
    # to mask the null with the legacy "bedrock_claude" fallback).
    provider_choice = account.get("provider_choice")
    if provider_choice is None:
        return Gate(
            code="provider_required",
            title="Pick how to power your agents",
            message=("Choose Bedrock-managed credits, ChatGPT OAuth, or your own API key to continue."),
            action_label="Pick provider",
            action_href="/chat?panel=llm&action=change-provider",
            # Personal owners are always admin of themselves; org members
            # can't change the org's provider — only the admin can.
            action_admin_only=True,
            owner_role=owner_role,
        )

    # Layer 3 — provider-specific prerequisites.
    if provider_choice == "bedrock_claude":
        # Credits pool at the owner level — org members all draw from one
        # balance funded by the admin. clerk_user_id stays per-user for
        # the OAuth-tokens probe below.
        balance = await credit_ledger.get_balance(owner_id)
        if balance <= 0:
            return Gate(
                code="credits_required",
                title="Top up Claude credits to start your container",
                message="Top up some Claude credits to start your Bedrock container.",
                action_label="Top up now",
                action_href="/settings/billing#credits",
                action_admin_only=False,
                owner_role=owner_role,
            )

    if provider_choice == "chatgpt_oauth":
        if not await _has_oauth_tokens(clerk_user_id):
            return Gate(
                code="oauth_required",
                title="Sign in with ChatGPT",
                message="Complete the ChatGPT sign-in to start your container.",
                action_label="Sign in with ChatGPT",
                action_href="/chat?provider=chatgpt_oauth",
                action_admin_only=False,
                owner_role=owner_role,
            )

    if provider_choice == "byo_key":
        # Symmetric with the chatgpt_oauth gate above: the BYO path has a
        # provider sub-choice (Anthropic / OpenAI) that the user picks
        # AFTER selecting "byo_key". Until that sub-choice is persisted,
        # ECS provisioning rejects with
        # "byo_provider is required when provider_choice == 'byo_key'".
        # The legacy ``POST /container/provision`` flow had a frontend
        # ``needsProviderStep`` guard for this; the new poll-driven
        # /container/status trigger lost the guard, so we enforce it
        # here as the canonical gate site.
        if not account.get("byo_provider"):
            return Gate(
                code="byo_provider_required",
                title="Choose your BYO provider",
                message="Choose Anthropic or OpenAI as your BYO provider to start your container.",
                action_label="Choose provider",
                action_href="/chat?provider=byo_key",
                action_admin_only=False,
                owner_role=owner_role,
            )

    return None  # all gates pass — provisioning allowed
