"""Tests for the provision-gate helper."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.provision_gate import (
    Gate,
    evaluate_provision_gate,
    has_always_on_entitlement,
    is_paid_subscription,
    is_subscription_active,
    is_trial_blocked,
)


@pytest.mark.asyncio
async def test_non_org_owner_returns_workspace_required():
    """Personal workspaces no longer exist: a user_ owner id is refused with
    workspace_required before any billing read — the one gate a server-side
    door (wake, webhook, admin probe) handed a stale user_ id can see."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        gate = await evaluate_provision_gate(owner_id="user_x")
    assert gate is not None
    assert gate.code == "workspace_required"
    repo.get_by_owner_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_billing_row_is_no_gate_the_workspace_is_just_not_provisioned_yet():
    """Voice: no billing row means the provision has not written the trial yet. Nothing has run
    and nothing can spend, so the status read says pending, never a paywall (a member's read
    that beat the organization.created webhook used to see "Set up workspace billing")."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        assert await evaluate_provision_gate(owner_id="org_xyz", is_admin=False) is None


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
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "stripe_subscription_id": "sub_x",
                "subscription_status": status,
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
        )
    assert gate is not None
    assert gate.code == "subscription_required"
    assert gate.action_href == "/settings/billing"


@pytest.mark.asyncio
async def test_org_abandoned_team_checkout_returns_team_billing_required():
    """An org that STARTED /billing/team-checkout and abandoned it has a
    customer row (create_customer_for_owner writes stripe_customer_id before
    the Stripe tab) with no subscription_status. It routes back to the
    workspace paywall (team_billing_required, /settings/billing), from which
    checkout can be retried (the 409 guard excepts a None-status row)."""
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
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_x",
                "stripe_customer_id": "cus_x",
                "subscription_status": "canceled",
                # cancel_subscription clears stripe_subscription_id —
                # the no-sub-id check alone is not enough.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
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
                "provider_choice": "openrouter_managed",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
        )
    assert gate is not None
    assert gate.code == "credits_required"


@pytest.mark.asyncio
async def test_active_managed_openrouter_owner_blocked_returns_credits_required():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "openrouter_managed",
            }
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is not None
    assert gate.code == "credits_required"
    assert "Lightbulb credits" in gate.message


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
                "provider_choice": "openrouter_managed",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
        )
    assert gate is None  # all gates pass


@pytest.mark.asyncio
async def test_trial_local_no_provider_choice_returns_none():
    """A trial_local row has NO provider_choice on it — its provider is
    resolved to `meta` elsewhere, never stored on the billing row. The
    Layer-2 provider_required check must NOT fire for trial_local; the
    gate short-circuits to None (provisioning allowed) before Layer 2."""
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                # No provider_choice, no stripe_subscription_id — neither
                # is written for the no-card local trial.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
        )
    assert gate is None  # Layer-2 provider_required must not fire


@pytest.mark.asyncio
async def test_trial_local_over_cap_returns_trial_daily_cap():
    """A trial_local owner the openrouter-gate stamped capped today gets the
    trial_daily_cap blocked gate (the daily-limit card) instead of a None
    (provision-eligible) result — so /container/status shows the limit and
    never (re)provisions the container."""
    from core.services.trial_cap import utc_today

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local", "openrouter_capped_day": utc_today()}
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is not None
    assert gate.code == "trial_daily_cap"
    assert gate.action_admin_only is False


@pytest.mark.asyncio
async def test_trial_local_expired_returns_trial_expired_gate():
    """A trial_local row whose trial_end has passed must block with the
    dedicated trial_expired gate — BEFORE the daily-cap check runs."""
    import time

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                "trial_end": int(time.time()) - 60,  # 1 minute ago
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is not None
    assert gate.code == "trial_expired"
    assert gate.title == "This workspace is reading-only"
    assert gate.action_href == "/settings?panel=billing"


@pytest.mark.asyncio
async def test_trial_local_future_trial_end_cap_logic_unchanged_under_cap():
    """trial_end in the future must fall through to the existing cap
    check unmodified: under-cap spend still returns None."""
    import time

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                "trial_end": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is None


@pytest.mark.asyncio
async def test_trial_local_future_trial_end_cap_logic_unchanged_over_cap():
    """trial_end in the future + capped today still returns
    trial_daily_cap, not trial_expired."""
    import time
    from core.services.trial_cap import utc_today

    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                "trial_end": int(time.time()) + 3600,
                "openrouter_capped_day": utc_today(),
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is not None
    assert gate.code == "trial_daily_cap"


@pytest.mark.asyncio
async def test_trial_local_missing_trial_end_not_expired():
    """A trial_local row with no trial_end at all (legacy dev row created
    before trial_end was stamped) must NOT be treated as expired — the
    daily cap still bounds it, so an uncapped day returns None."""
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trial_local",
                # No trial_end field.
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("blocked", "code"), [(True, "credits_required"), (False, None)])
async def test_a_cleared_provider_choice_is_judged_on_the_credits_it_will_draw(blocked, code):
    """No stored provider on an uncapped live row (a ChatGPT disconnect cleared it) resolves to
    managed credits (provider_choice_from_row), so the gate checks those credits instead of the
    old "Pick how to power your agents" card, which blocked a workspace that would run."""
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=blocked)),
    ):
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active", "stripe_subscription_id": "s"})
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert (gate.code if gate else None) == code


@pytest.mark.asyncio
async def test_a_live_trial_runs_chat_and_orb_without_a_plan_or_provider():
    """The voice contract: a fresh trial's chat and orb work (phone is go-live's job)."""
    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local", "trial_end": None})
        assert await evaluate_provision_gate(owner_id="org_x") is None


@pytest.mark.asyncio
async def test_a_trial_with_a_stray_chatgpt_choice_is_judged_as_the_trial_it_runs_as():
    """A trial row runs on managed inference whatever provider_choice it carries, so no
    ChatGPT sign-in card for it: the trial predicate alone."""
    oauth = AsyncMock(return_value=False)
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.provision_gate._has_oauth_tokens", oauth),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local", "provider_choice": "chatgpt_oauth"}
        )
        assert await evaluate_provision_gate(owner_id="org_x") is None
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_past_due_returns_payment_past_due():
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "past_due", "stripe_subscription_id": "sub_x"},
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
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
                "provider_choice": "openrouter_managed",
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
            owner_id="org_x",
        )
    assert gate is not None
    assert gate.code == "oauth_required"
    # Click target must be a real route. /settings/llm doesn't exist on
    # the frontend; the canonical OAuth completion path is the
    # ?provider=chatgpt_oauth wizard mounted on /chat.
    assert gate.action_href == "/chat?provider=chatgpt_oauth"


@pytest.mark.asyncio
async def test_gate_rejects_connector_row_bound_to_other_workspace():
    """THE P1 GATE GUARD. The workspace's connector (user_x) has a token row,
    but it is bound to ANOTHER workspace (they connected ChatGPT there under
    the same Clerk user). The gate must pass owner_id (org_x) as bound_to so
    that row reads as no-tokens and oauth_required fires — otherwise this
    container provisions on tokens that back the other workspace's container.
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
                "owner_id": "org_x",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "chatgpt_oauth",
                "oauth_connector_user_id": "user_x",
            },
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
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

    Symmetric with the chatgpt_oauth gate.
    """
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trialing",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "byo_key",
                # No byo_provider on the row — sub-choice not made yet.
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
            is_admin=True,
        )
    assert gate is not None
    assert gate.code == "byo_provider_required"
    # An incomplete legacy row needs an admin to pick a supported provider.
    assert gate.action_href == "/settings?panel=billing"
    # New key setup is closed.
    assert gate.action_admin_only is True


@pytest.mark.asyncio
async def test_byo_key_with_byo_provider_set_passes():
    """Once byo_provider is set (Anthropic or OpenAI), the gate clears
    and provisioning is allowed. The downstream BYO-key existence check
    is the responsibility of ECS provisioning, not this gate — this
    layer only enforces "the sub-choice was made"."""
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.trial_cap.billing_repo", repo),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "trialing",
                "stripe_subscription_id": "sub_x",
                "provider_choice": "byo_key",
                "byo_provider": "anthropic",
            },
        )
        gate = await evaluate_provision_gate(
            owner_id="org_x",
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
# Pure predicates: is_subscription_active / is_paid_subscription /
# is_trial_blocked
# ---------------------------------------------------------------------------
#
# Every gate site (chat, provision, channel-binding) consults
# ``is_subscription_active``; the /billing/account response's
# ``is_subscribed`` wire field consults ``is_paid_subscription`` (the paid
# axis — trial_local is usable but NOT subscribed). Pinning every Stripe
# status case here is what locks each predicate's contract.
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
        # Missing / empty rows — fail-closed.
        (None, False),
        ({}, False),
        # Paid states.
        ({"subscription_status": "active"}, True),
        ({"subscription_status": "trialing"}, True),
        # *** THE DIVERGENCE CASE ***
        # trial_local grants platform use (is_subscription_active → True)
        # but is NOT a subscription. Wiring /billing/account's
        # ``is_subscribed`` from the platform-use predicate rendered free
        # users an "Active subscription — $50/month" card with no way to
        # subscribe; this row pins the paid predicate to False.
        ({"subscription_status": "trial_local"}, False),
        # Non-paid explicit states.
        ({"subscription_status": "past_due"}, False),
        ({"subscription_status": "canceled"}, False),
        ({"subscription_status": "unpaid"}, False),
        ({"subscription_status": "paused"}, False),
        # Legacy fallback: status not yet backfilled, sub_id present —
        # same strict None-status guard as is_subscription_active.
        ({"subscription_status": None, "stripe_subscription_id": "sub_legacy"}, True),
        ({"subscription_status": None, "stripe_subscription_id": None}, False),
        ({"subscription_status": None}, False),
        # An explicit non-paid status never rides a stale sub_id into paid.
        (
            {"subscription_status": "canceled", "stripe_subscription_id": "sub_stale"},
            False,
        ),
        (
            {"subscription_status": "trial_local", "stripe_subscription_id": "sub_x"},
            False,
        ),
    ],
)
def test_is_paid_subscription_table(account, expected):
    assert is_paid_subscription(account) is expected


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


class TestHasAlwaysOnEntitlement:
    """Paid-only axis for always-on features (webhooks, channels, routines).

    Distinct from ``is_subscription_active``: ``trial_local`` grants
    platform use (chat) but must never grant always-on entitlement.
    """

    def test_active_paid_row_is_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "active"}) is True

    def test_stripe_trialing_row_is_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "trialing"}) is True

    def test_trial_local_is_not_entitled(self):
        # trial_local passes is_subscription_active (chat is free) but must
        # NEVER be always-on entitled — the two predicates are different axes.
        assert has_always_on_entitlement({"subscription_status": "trial_local"}) is False

    def test_missing_row_is_not_entitled(self):
        assert has_always_on_entitlement(None) is False

    def test_empty_row_is_not_entitled(self):
        assert has_always_on_entitlement({}) is False

    def test_canceled_is_not_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "canceled"}) is False

    def test_legacy_row_with_subscription_id_is_entitled(self):
        # mid-cutover rows: no clean status but a live Stripe sub id —
        # mirror is_subscription_active's legacy fallback.
        assert has_always_on_entitlement({"stripe_subscription_id": "sub_123"}) is True

    def test_canceled_with_stale_subscription_id_is_not_entitled(self):
        # The legacy fallback only applies when status is None — an explicit
        # non-paid status must never ride a leftover sub id into entitlement.
        assert (
            has_always_on_entitlement(
                {"subscription_status": "canceled", "stripe_subscription_id": "sub_stale"},
            )
            is False
        )

    def test_trial_local_with_stray_subscription_id_is_not_entitled(self):
        # trial_local is definitionally unpaid — even a malformed row that
        # somehow carries a sub id must not become always-on entitled.
        assert (
            has_always_on_entitlement(
                {"subscription_status": "trial_local", "stripe_subscription_id": "sub_x"},
            )
            is False
        )


@pytest.mark.asyncio
async def test_probe_provision_gate_skips_metric_emission(monkeypatch):
    """probe_provision_gate returns the same Gate as evaluate_provision_gate
    but must NOT emit provision.gate.blocked (the W64 alarm stream)."""
    from core.services import provision_gate

    sentinel = provision_gate.Gate(
        code="subscription_required",
        title="t",
        message="m",
        action_label="a",
        action_href="/settings/billing",
        action_admin_only=False,
        owner_role="admin",
    )

    async def fake_inner(*, owner_id, is_admin):
        return sentinel

    emitted = []
    monkeypatch.setattr(provision_gate, "_evaluate_provision_gate_inner", fake_inner)
    # Record every telemetry path evaluate_provision_gate would have taken:
    # put_metric (the W64 alarm stream + coded breakdown) and the PostHog
    # trial_expired capture. The probe must touch neither.
    monkeypatch.setattr(provision_gate, "put_metric", lambda *a, **kw: emitted.append(("put_metric", a, kw)))
    monkeypatch.setattr(
        provision_gate,
        "capture_once_per_owner_per_day",
        lambda *a, **kw: emitted.append(("capture", a, kw)),
    )

    gate = await provision_gate.probe_provision_gate(owner_id="org_x")
    assert gate is sentinel
    assert emitted == []


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


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["base", "standard", "ultra_pro"])
async def test_capped_plan_without_provider_skips_picker_and_credits(plan):
    """Base, and (interim) Workspace/Ultra with no stored choice, run on the
    capped managed model: no provider picker, no Metronome credits check."""
    blocked = AsyncMock(return_value=True)
    with (
        patch("core.services.provision_gate.billing_repo") as repo,
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "active", "stripe_subscription_id": "sub_x", "plan": plan}
        )
        assert await evaluate_provision_gate(owner_id="org_x") is None
    blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_capped_plan_over_its_daily_limit_returns_included_daily_cap():
    from core.services.trial_cap import utc_today

    with patch("core.services.provision_gate.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_x",
                "plan": "base",
                "openrouter_capped_day": utc_today(),
            }
        )
        gate = await evaluate_provision_gate(owner_id="org_x")
    assert gate is not None and gate.code == "included_daily_cap"


def test_included_daily_cap_is_its_own_card_not_the_trial_copy():
    """Owner ruling 2026-09-22: a paying Base/paid-default owner at the cap must not see the
    trial "Daily free limit reached ... Upgrade" card."""
    from core.services import provision_gate, trial_cap

    gate = provision_gate.gate_for_trial_reason(trial_cap.INCLUDED_CAP, "member")
    assert "free" not in (gate.title + gate.message).lower()
