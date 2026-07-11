"""Tests for the provision-gate helper."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.provision_gate import (
    Gate,
    evaluate_provision_gate,
    is_subscription_active,
    is_trial_blocked,
)


@pytest.mark.asyncio
async def test_no_billing_account_returns_signup_required():
    """No billing row at all — never started signup. Distinct code from
    subscription_required (which is for inactive existing rows) so the
    /container/status endpoint can route admins to the ProviderPicker
    without short-circuiting the canceled/unpaid/paused path."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "signup_required"
    assert gate.action_admin_only is True


@pytest.mark.asyncio
async def test_no_billing_org_owner_returns_team_billing_required():
    """An org owner (owner_id starts with 'org_') with no billing row gets a
    DISTINCT team_billing_required code, not the personal signup_required. The
    free tier is personal-only, so a team never falls into a card-less
    trial_local — an org with no billing needs per-seat Teams billing, and the
    frontend keys the Teams paywall off this code rather than the personal
    provider picker."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        gate = await evaluate_provision_gate(owner_id="org_xyz", is_admin=True)
    assert gate is not None
    assert gate.code == "team_billing_required"
    assert gate.action_admin_only is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused"],
)
async def test_inactive_billing_account_with_subscription_id_returns_subscription_required(status):
    """Existing billing row WITH a stripe_subscription_id but inactive
    status keeps the subscription_required code (action:
    /settings/billing) so the frontend routes the user to the billing
    portal — *not* to the picker, which would 409 already_subscribed."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "stripe_subscription_id": "sub_x",
                "subscription_status": status,
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "subscription_required"
    assert gate.action_href == "/settings/billing"


@pytest.mark.asyncio
async def test_billing_account_without_subscription_id_returns_signup_required():
    """/billing/trial-checkout creates the billing-account row BEFORE the
    Stripe Checkout session, so a user who
    cancels Stripe (or whose checkout-session creation fails after the
    row is written) lands here with an account but no
    stripe_subscription_id and no subscription_status. /settings/billing
    has nothing to manage in that case — the gate must fire
    signup_required so the frontend re-renders the picker, which
    trial-checkout will accept (is_trial_blocked is False without a
    Stripe-side status)."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_x",
                "stripe_customer_id": "cus_x",
                # provider_choice may be set (trial-checkout writes it
                # synchronously before creating the Checkout session).
                "provider_choice": "bedrock_claude",
                # No stripe_subscription_id, no subscription_status —
                # checkout was abandoned before Stripe issued a sub.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "signup_required"
    assert gate.action_href == "/onboarding"


@pytest.mark.asyncio
async def test_org_abandoned_team_checkout_returns_team_billing_required():
    """An org that STARTED /billing/team-checkout and abandoned it has a
    customer row (create_customer_for_owner writes stripe_customer_id before
    the Stripe tab) with no subscription_status. The personal companion above
    routes such a None-status row to signup_required (/onboarding) — but an
    org must NOT be funneled into the personal picker. It routes back to the
    Teams paywall (team_billing_required, /settings/billing), from which
    team-checkout can be retried (the 409 guard excepts a None-status row)."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_abandoned",
                "stripe_customer_id": "cus_org",
                # No stripe_subscription_id, no subscription_status —
                # team checkout was abandoned before Stripe issued a sub.
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_abandoned", is_admin=True)
    assert gate is not None
    assert gate.code == "team_billing_required"
    assert gate.action_href == "/settings/billing"
    assert gate.action_admin_only is True


@pytest.mark.asyncio
async def test_canceled_subscription_with_cleared_subscription_id_routes_to_billing():
    """The in-app cancel path (BillingService.cancel_subscription) clears
    stripe_subscription_id
    but leaves subscription_status='canceled'. is_trial_blocked still
    rejects 'canceled' rows in /billing/trial-checkout, so we must NOT
    send these admins to the picker — they'd hit 409 already_subscribed.
    Route to /settings/billing instead, matching is_trial_blocked."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_x",
                "stripe_customer_id": "cus_x",
                "subscription_status": "canceled",
                # cancel_subscription clears stripe_subscription_id —
                # the no-sub-id check alone is not enough.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "subscription_required"
    assert gate.action_href == "/settings/billing"


@pytest.mark.asyncio
async def test_active_subscription_bedrock_blocked_returns_credits_required():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "bedrock_claude",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "credits_required"


@pytest.mark.asyncio
async def test_trialing_with_credits_returns_none():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=False)),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trialing",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "bedrock_claude",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is None  # all gates pass


@pytest.mark.asyncio
async def test_trial_local_no_provider_choice_returns_none():
    """A trial_local row has NO provider_choice on it — its provider is
    resolved to `meta` elsewhere, never stored on the billing row. The
    Layer-2 provider_required check must NOT fire for trial_local; the
    gate short-circuits to None (provisioning allowed) before Layer 2."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                # No provider_choice, no stripe_subscription_id — neither
                # is written for the no-card local trial.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is None  # Layer-2 provider_required must not fire


@pytest.mark.asyncio
async def test_active_subscription_no_provider_choice_returns_provider_required():
    """Active subscription + provider_choice absent (e.g. ChatGPT disconnect cleared it,
    or the row pre-dates provider_choice writes) must surface provider_required
    so the frontend routes the owner to the picker, not the credits/oauth/byo
    gates which would silently treat the legacy fallback as Bedrock.
    """
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                # No provider_choice on the row.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "provider_required"
    # Clicking the action must land on the LLM panel with the change-provider
    # modal pre-opened. ChatErrorCard's rate_limit action shares this URL.
    assert gate.action_href == "/chat?panel=llm&action=change-provider"
    # Provider choice is an admin-only mutation for orgs; personal owners
    # are admin of themselves so the action button still renders for them.
    assert gate.action_admin_only is True


@pytest.mark.asyncio
async def test_past_due_returns_payment_past_due():
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "past_due", "stripe_subscription_id": "sub_x"},
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "payment_past_due"


@pytest.mark.asyncio
async def test_bedrock_balance_keyed_on_owner_id():
    """Org members hitting /onboarding's pre-provision check must read the
    pooled org balance, not their personal user_id row (which is empty
    after the org-pooled-credits cutover)."""
    blocked_mock = AsyncMock(return_value=False)
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked_mock),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "bedrock_claude",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_Y",
        )
    assert gate is None  # gate passes
    blocked_mock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_chatgpt_oauth_no_tokens_returns_oauth_required():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._has_oauth_tokens", new_callable=AsyncMock) as ht,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
            },
        )
        ht.return_value = False
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "oauth_required"
    # Click target must be a real route. /settings/llm doesn't exist on
    # the frontend; the canonical OAuth completion path is the
    # ?provider=chatgpt_oauth wizard mounted on /chat.
    assert gate.action_href == "/chat?provider=chatgpt_oauth"


@pytest.mark.asyncio
async def test_personal_gate_rejects_row_bound_to_other_workspace():
    """THE P1 GATE GUARD. A personal owner (user_x) has a token row, but it is
    bound to an ORG (they connected ChatGPT for an org under the same Clerk
    user). The personal gate must pass owner_id (user_x) as bound_to so the
    org-bound row reads as no-tokens and oauth_required fires — otherwise the
    personal container provisions on tokens that back the org container.
    """

    async def active_for(*, user_id: str, bound_to: str | None = None):
        # Row exists for user_x but bound to org_other; only a bound_to that
        # matches (or None) reads as active. The gate passes bound_to=owner_id.
        if user_id != "user_x":
            return False
        bound_owner = "org_other"
        if bound_to is not None and bound_to != bound_owner:
            return False
        return True

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch(
            "core.services.oauth_service.has_active_oauth",
            new=AsyncMock(side_effect=active_for),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_x",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
            },
        )
        gate = await evaluate_provision_gate(owner_id="user_x")
    assert gate is not None
    assert gate.code == "oauth_required"


@pytest.mark.asyncio
async def test_org_gate_passes_when_connector_row_bound_to_org():
    """An org's connector (user_a) has a row bound to the org; the gate reads
    with bound_to=org_1 and finds it → passes. Pins that the org branch passes
    owner_id as bound_to and a correctly-bound row still satisfies the gate."""

    async def active_for(*, user_id: str, bound_to: str | None = None):
        if user_id != "user_a":
            return False
        bound_owner = "org_1"
        if bound_to is not None and bound_to != bound_owner:
            return False
        return True

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch(
            "core.services.oauth_service.has_active_oauth",
            new=AsyncMock(side_effect=active_for),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
                "oauth_connector_user_id": "user_a",
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_1", is_admin=True)
    assert gate is None  # connector row bound to the org → gate passes


@pytest.mark.asyncio
async def test_org_gate_checks_connector_tokens_not_poller():
    """In an org, the gate must read the CONNECTOR's tokens (the owner who
    completed the device-code flow, recorded as oauth_connector_user_id),
    not the polling user's. Owner user_a connected; admin user_b polls —
    the gate must pass for user_b even though user_b has no OAuth row.

    Without this, the gate flaps per-poller: every org member that hasn't
    personally signed in to ChatGPT gets told to "sign in" on the shared
    container that user_a already powers.
    """

    # Tokens exist only for the connector (user_a); anyone else (the poller)
    # has none. Keying the mock on user_id pins that the gate reads the
    # connector, not the clerk_user_id passed in.
    async def active_for(*, user_id: str, bound_to: str | None = None):
        return user_id == "user_a"

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch(
            "core.services.oauth_service.has_active_oauth",
            new=AsyncMock(side_effect=active_for),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
                "oauth_connector_user_id": "user_a",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_1",
            is_admin=True,
        )
    assert gate is None  # connector connected → gate passes for the poller


@pytest.mark.asyncio
async def test_org_gate_oauth_required_is_admin_only():
    """No connector recorded and nobody connected → oauth_required. For an
    org the bind-a-ChatGPT-account action is admin-only (only the owner may
    attach an account to the shared container), so a non-admin member sees
    the gate but not an actionable button.
    """

    async def active_for(*, user_id: str, bound_to: str | None = None):
        return False  # nobody has an active connection

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch(
            "core.services.oauth_service.has_active_oauth",
            new=AsyncMock(side_effect=active_for),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
                # No oauth_connector_user_id — falls back to owner_id (org_1),
                # which has no tokens.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_1",
            is_admin=False,
        )
    assert gate is not None
    assert gate.code == "oauth_required"
    assert gate.action_admin_only is True


@pytest.mark.asyncio
async def test_personal_gate_oauth_required_is_not_admin_only():
    """Personal owner with no tokens still gets oauth_required, but the
    action stays member-actionable (action_admin_only False) — owner_id
    doesn't start with 'org_', and a personal user is admin of themselves
    so the sign-in button must render for them.
    """

    async def active_for(*, user_id: str, bound_to: str | None = None):
        return False

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch(
            "core.services.oauth_service.has_active_oauth",
            new=AsyncMock(side_effect=active_for),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_x",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
        )
    assert gate is not None
    assert gate.code == "oauth_required"
    assert gate.action_admin_only is False


@pytest.mark.asyncio
async def test_byo_key_without_byo_provider_returns_byo_provider_required():
    """provider_choice='byo_key' + byo_provider=None must block ECS
    dispatch with code='byo_provider_required'.

    The legacy ``POST /container/provision`` flow had a frontend
    ``needsProviderStep`` guard for the BYO sub-choice (Anthropic/OpenAI).
    The new poll-driven /container/status canonical trigger lost that
    guard, so without this gate the user gets stuck in container/error
    after picking BYO but not yet selecting a sub-provider — ECS
    rejects with "byo_provider is required when provider_choice ==
    'byo_key'".

    Symmetric with the chatgpt_oauth gate for personal owners.
    """
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trialing",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "byo_key",
                # No byo_provider on the row — sub-choice not made yet.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            is_admin=True,
        )
    assert gate is not None
    assert gate.code == "byo_provider_required"
    # Click target must be a real route. /settings/llm doesn't exist on
    # the frontend; the canonical BYO sub-choice path is the
    # ?provider=byo_key wizard mounted on /chat.
    assert gate.action_href == "/chat?provider=byo_key"
    # Member-actionable: the user must pick anthropic/openai themselves;
    # admin-only would mis-block the action UI for owners who are admin
    # of themselves (personal users).
    assert gate.action_admin_only is False


@pytest.mark.asyncio
async def test_byo_key_with_byo_provider_set_passes():
    """Once byo_provider is set (Anthropic or OpenAI), the gate clears
    and provisioning is allowed. The downstream BYO-key existence check
    is the responsibility of ECS provisioning, not this gate — this
    layer only enforces "the sub-choice was made"."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trialing",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "byo_key",
                "byo_provider": "anthropic",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            is_admin=True,
        )
    assert gate is None  # all gates pass


def test_gate_to_payload_shape():
    gate = Gate(
        code="credits_required",
        title="Top up Claude credits",
        message="Top up some Claude credits to start your Bedrock container.",
        action_label="Top up now",
        action_href="/settings/billing#credits",
        action_admin_only=False,
        owner_role="admin",
    )
    payload = gate.to_payload()
    assert payload["blocked"]["code"] == "credits_required"
    assert payload["blocked"]["action"]["href"] == "/settings/billing#credits"
    assert payload["blocked"]["action"]["admin_only"] is False
    assert payload["blocked"]["owner_role"] == "admin"
    assert payload["detail"]  # legacy string preserved


# ---------------------------------------------------------------------------
# Pure predicates: is_subscription_active / is_trial_blocked
# ---------------------------------------------------------------------------
#
# Every gate site (chat, provision, channel-binding, /billing/account
# response) consults ``is_subscription_active``. Pinning every Stripe
# status case here is what locks the predicate's contract.
#
# The single load-bearing case is ``canceled + stale stripe_subscription_id``
# at the bottom of the table — it's the convergence case from the
# 2026-05-04 audit. Before the predicate was extracted, the
# /billing/account response returned ``is_subscribed=True`` for this row
# while the chat / provision / config gates returned ``is_ok=False``.
# That divergence is what this test class prevents from drifting back in.


@pytest.mark.parametrize(
    "account, expected",
    [
        # Missing / empty rows.
        (None, False),
        ({}, False),
        # Active states.
        ({"subscription_status": "active"}, True),
        ({"subscription_status": "trialing"}, True),
        # No-card local trial — a Stripe-less trial state that must grant
        # platform use the same as "trialing" (unblocks chat + Layer 1 of
        # the provision gate).
        ({"subscription_status": "trial_local"}, True),
        # Inactive states — none of these grant access regardless of
        # whether ``stripe_subscription_id`` is still set on the row.
        ({"subscription_status": "past_due"}, False),
        ({"subscription_status": "canceled"}, False),
        ({"subscription_status": "incomplete"}, False),
        ({"subscription_status": "incomplete_expired"}, False),
        ({"subscription_status": "unpaid"}, False),
        ({"subscription_status": "paused"}, False),
        ({"subscription_status": "unknown_future_state"}, False),
        # Legacy fallback: status not yet backfilled, sub_id present.
        ({"subscription_status": None, "stripe_subscription_id": "sub_legacy"}, True),
        # Legacy fallback NEGATIVE: sub_id missing → not subscribed.
        ({"subscription_status": None, "stripe_subscription_id": None}, False),
        ({"subscription_status": None}, False),
        # *** LOAD-BEARING REGRESSION CASE ***
        # canceled status + stale stripe_subscription_id must NOT count
        # as subscribed. Pre-convergence, the /billing/account response
        # site reported True for this row (status-less ``or has_legacy_sub``
        # fallback) while every gate site reported False.
        (
            {"subscription_status": "canceled", "stripe_subscription_id": "sub_stale"},
            False,
        ),
        # Same shape on past_due / paused — leftover sub_id never
        # overrides a non-active explicit status.
        (
            {"subscription_status": "past_due", "stripe_subscription_id": "sub_x"},
            False,
        ),
        (
            {"subscription_status": "paused", "stripe_subscription_id": "sub_x"},
            False,
        ),
    ],
)
def test_is_subscription_active_table(account, expected):
    assert is_subscription_active(account) is expected


@pytest.mark.parametrize(
    "account, expected",
    [
        # No row → trial allowed (the canonical "fresh signup" path).
        (None, False),
        ({}, False),
        ({"subscription_status": None}, False),
        # Every state in TRIAL_BLOCKED_STATUSES blocks fresh trials.
        ({"subscription_status": "active"}, True),
        ({"subscription_status": "trialing"}, True),
        ({"subscription_status": "past_due"}, True),
        ({"subscription_status": "canceled"}, True),
        ({"subscription_status": "incomplete"}, True),
        ({"subscription_status": "incomplete_expired"}, True),
        ({"subscription_status": "unpaid"}, True),
        ({"subscription_status": "paused"}, True),
        # trial_local must NOT block a fresh trial-creation attempt — a
        # later paid upgrade (real Stripe trial/subscription) must stay
        # possible for an account currently in the no-card local trial.
        ({"subscription_status": "trial_local"}, False),
        # Unknown/future status doesn't block — fail-open is the right
        # choice for the trial-checkout gate (worst case is one extra
        # trial; cancel-loop is gated by Stripe support workflow).
        ({"subscription_status": "unknown_future_state"}, False),
    ],
)
def test_is_trial_blocked_table(account, expected):
    assert is_trial_blocked(account) is expected


@pytest.mark.asyncio
async def test_has_oauth_tokens_uses_state_check_not_decrypt():
    """The connected-check must not decrypt the token blob — it only needs to
    know an active, owner-bound row exists."""
    from core.services import provision_gate

    with (
        patch("core.services.oauth_service.has_active_oauth", new=AsyncMock(return_value=True)) as active,
        patch("core.services.oauth_service.get_decrypted_tokens", new=AsyncMock()) as decrypt,
    ):
        result = await provision_gate._has_oauth_tokens("user_c", "org_1")
    assert result is True
    active.assert_awaited_once_with(user_id="user_c", bound_to="org_1")
    decrypt.assert_not_called()
