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
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "signup_required"
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
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "subscription_required"
    assert gate.action_href == "/settings/billing"


@pytest.mark.asyncio
async def test_billing_account_without_subscription_id_returns_signup_required():
    """Codex P1 on PR #570: /billing/trial-checkout creates the billing-
    account row BEFORE the Stripe Checkout session, so a user who
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
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "signup_required"
    assert gate.action_href == "/onboarding"


@pytest.mark.asyncio
async def test_canceled_subscription_with_cleared_subscription_id_routes_to_billing():
    """Codex P1 round 2 on PR #570: the in-app cancel path
    (BillingService.cancel_subscription) clears stripe_subscription_id
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
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "subscription_required"
    assert gate.action_href == "/settings/billing"


@pytest.mark.asyncio
async def test_active_subscription_bedrock_zero_balance_returns_credits_required():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate.credit_ledger") as cl,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "active", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "bedrock_claude"
        cl.get_balance = AsyncMock(return_value=0)
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "credits_required"


@pytest.mark.asyncio
async def test_trialing_with_credits_returns_none():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate.credit_ledger") as cl,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trialing", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "bedrock_claude"
        cl.get_balance = AsyncMock(return_value=500_000)  # 50 cents in microcents
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
        )
    assert gate is None  # all gates pass


@pytest.mark.asyncio
async def test_past_due_returns_payment_past_due():
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "past_due", "stripe_subscription_id": "sub_x"},
        )
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "payment_past_due"


@pytest.mark.asyncio
async def test_bedrock_balance_keyed_on_owner_id():
    """Org members hitting /onboarding's pre-provision check must read the
    pooled org balance, not their personal user_id row (which is empty
    after the org-pooled-credits cutover)."""
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate.credit_ledger") as cl,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "active", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "bedrock_claude"
        balance_mock = AsyncMock(return_value=5_000_000)
        cl.get_balance = balance_mock
        gate = await evaluate_provision_gate(
            owner_id="org_Y",
            clerk_user_id="user_member_X",
        )
    assert gate is None  # gate passes
    args, _ = balance_mock.call_args
    assert args[0] == "org_Y"


@pytest.mark.asyncio
async def test_chatgpt_oauth_no_tokens_returns_oauth_required():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate._has_oauth_tokens", new_callable=AsyncMock) as ht,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "active", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "chatgpt_oauth"
        ht.return_value = False
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
        )
    assert gate is not None
    assert gate.code == "oauth_required"
    # Click target must be a real route. /settings/llm doesn't exist on
    # the frontend; the canonical OAuth completion path is the
    # ?provider=chatgpt_oauth wizard mounted on /chat.
    assert gate.action_href == "/chat?provider=chatgpt_oauth"


@pytest.mark.asyncio
async def test_byo_key_without_byo_provider_returns_byo_provider_required():
    """Codex P1 (Finding D): provider_choice='byo_key' + byo_provider=None
    must block ECS dispatch with code='byo_provider_required'.

    The legacy ``POST /container/provision`` flow had a frontend
    ``needsProviderStep`` guard for the BYO sub-choice (Anthropic/OpenAI).
    The new poll-driven /container/status canonical trigger lost that
    guard, so without this gate the user gets stuck in container/error
    after picking BYO but not yet selecting a sub-provider — ECS
    rejects with "byo_provider is required when provider_choice ==
    'byo_key'".

    Symmetric with the chatgpt_oauth gate for personal owners.
    """
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate._get_byo_provider", new_callable=AsyncMock) as gbp,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trialing", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "byo_key"
        gbp.return_value = None  # BYO sub-choice not made yet
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
            is_admin=True,
        )
    assert gate is not None
    assert gate.code == "byo_provider_required"
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
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._get_provider_choice", new_callable=AsyncMock) as gp,
        patch("core.services.provision_gate._get_byo_provider", new_callable=AsyncMock) as gbp,
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trialing", "stripe_subscription_id": "sub_x"},
        )
        gp.return_value = "byo_key"
        gbp.return_value = "anthropic"
        gate = await evaluate_provision_gate(
            owner_id="user_x",
            clerk_user_id="user_x",
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
        # Unknown/future status doesn't block — fail-open is the right
        # choice for the trial-checkout gate (worst case is one extra
        # trial; cancel-loop is gated by Stripe support workflow).
        ({"subscription_status": "unknown_future_state"}, False),
    ],
)
def test_is_trial_blocked_table(account, expected):
    assert is_trial_blocked(account) is expected
