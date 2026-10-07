"""Tests for /container/provision payment gating (C1 audit fix).

The primary `/container/provision` endpoint historically had no payment
check — any signed-in Clerk user could spin up an ECS Fargate task at
our cost. These tests pin down the gating contract:

  - New workspace with no billing row    -> unarmed trial, then provision
  - subscription_status not in {active, trialing}
                                         -> 402
  - openrouter_managed with $0 balance       -> 402  (no point spinning a
                                                   container the user
                                                   can't chat through)
  - openrouter_managed with > 0 balance      -> 200
  - chatgpt_oauth / byo_key (any balance) -> 200
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _auth(user_id: str = "user_paid", org_id: str | None = "org_paid") -> MagicMock:
    auth = MagicMock()
    auth.user_id = user_id
    auth.org_id = org_id
    # A bare MagicMock auto-creates ``is_org_context``/``is_org_admin`` as
    # truthy mocks. That makes ``resolve_owner_id`` (``org_id if is_org_context
    # else user_id``) return ``org_id``. Set them to match the real
    # ``AuthContext`` property semantics.
    auth.is_org_context = org_id is not None
    auth.is_org_admin = True
    return auth


@pytest.fixture(autouse=True)
def _stub_workspace_trial_default():
    with patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_direct_provision_initializes_workspace_trial_before_gate():
    """A first provision request cannot see the pre-initialization org gate."""
    from core.services.provision_gate import _team_billing_required_gate
    from routers import container as container_mod

    initialized = False

    async def initialize(_auth, owner_id):
        nonlocal initialized
        assert owner_id == "org_paid"
        initialized = True

    async def gate(**_kwargs):
        return None if initialized else _team_billing_required_gate("admin")

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(container_mod, "ensure_workspace_trial_default", new=initialize),
        patch.object(container_mod, "evaluate_provision_gate", new=gate),
        patch.object(container_mod, "dispatch_provision", new=AsyncMock()),
    ):
        result = await container_mod.container_provision(auth=_auth())

    assert initialized is True
    assert result["status"] == "provisioning"


@pytest.mark.asyncio
async def test_direct_retry_initializes_workspace_trial_before_gate():
    """The retry endpoint initializes the workspace before its provision gate."""
    from routers import container as container_mod

    initialized = False

    async def initialize(_auth, owner_id):
        nonlocal initialized
        assert owner_id == "org_paid"
        initialized = True

    async def assert_allowed(_owner_id, *, is_admin):
        assert initialized, "retry evaluated its provision gate before initializing the workspace trial"
        assert is_admin is True

    with (
        patch.object(container_mod, "ensure_workspace_trial_default", new=initialize),
        patch.object(container_mod, "_assert_provision_allowed", new=assert_allowed),
        patch.object(container_mod, "dispatch_provision", new=AsyncMock()),
        patch.object(
            container_mod.container_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "org_paid", "status": "error"}),
        ),
    ):
        result = await container_mod.container_retry(auth=_auth())

    assert initialized is True
    assert result == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused", "past_due"],
)
async def test_provision_rejects_inactive_subscription_status(status):
    """Subscription exists but status is non-active -> 402."""
    from routers.container import container_provision

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={
                "stripe_subscription_id": "sub_123",
                "subscription_status": status,
            },
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await container_provision(auth=_auth())
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_provision_rejects_bedrock_with_zero_balance():
    """openrouter_managed provider + $0 balance -> 402.

    A container spun up before credits are topped up is pure cost — the
    user can't actually chat through it (gate_chat blocks on $0). Refuse
    the provision instead of leaking ECS.
    """
    from routers.container import container_provision

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={
                "owner_id": "org_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "trialing",
                # provider_choice lives on billing_accounts.
                "provider_choice": "openrouter_managed",
            },
        ),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=True),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await container_provision(auth=_auth())
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_provision_allows_bedrock_with_positive_balance():
    """openrouter_managed with credits topped up -> provisions normally."""
    from routers.container import container_provision

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={
                "owner_id": "org_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "trialing",
                # provider_choice lives on billing_accounts.
                "provider_choice": "openrouter_managed",
            },
        ),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=False),
        ),
        patch("routers.container.dispatch_provision", new_callable=AsyncMock) as mock_dispatch,
    ):
        result = await container_provision(auth=_auth())
    assert result["status"] == "provisioning"
    mock_dispatch.assert_called_once_with("org_paid")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_choice", ["chatgpt_oauth", "byo_key"])
async def test_provision_skips_balance_check_for_non_bedrock(provider_choice):
    """chatgpt_oauth / byo_key never need a credit balance — their LLM
    costs land on the user's own provider account."""
    from routers.container import container_provision

    # Pre-import oauth_service so we can patch its function attribute directly,
    # which beats sys.modules-swapping for cross-test isolation (the lazy
    # `from core.services import oauth_service` inside the gate hits the
    # already-imported module via package attribute lookup).
    from core.services import oauth_service  # noqa: F401

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={
                "owner_id": "org_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "active",
                # provider_choice lives on billing_accounts.
                "provider_choice": provider_choice,
                # The byo_provider sub-choice gate blocks byo_key without
                # a sub-provider; this test isolates
                # the credit-balance behaviour, so seed a complete BYO row.
                "byo_provider": "anthropic" if provider_choice == "byo_key" else None,
            },
        ),
        patch(
            "core.services.oauth_service.has_active_oauth",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("routers.container.dispatch_provision", new_callable=AsyncMock),
    ):
        result = await container_provision(auth=_auth())
    assert result["status"] == "provisioning"


@pytest.mark.asyncio
async def test_provision_allows_legacy_row_with_subscription_id_but_no_status():
    """Legacy billing rows may have a valid stripe_subscription_id but no
    subscription_status backfilled yet. Other gates (gate_chat) preserve
    those — the provision gate must too, or we 402 paying customers
    mid-migration.

    openrouter_managed users still need credits regardless of legacy
    status, so this test uses chatgpt_oauth to isolate the legacy-row
    behavior.
    """
    from routers.container import container_provision

    from core.services import oauth_service  # noqa: F401

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={
                "owner_id": "org_paid",
                "stripe_subscription_id": "sub_legacy_pre_plan_3",
                # subscription_status deliberately absent / None
                # provider_choice lives on billing_accounts.
                "provider_choice": "chatgpt_oauth",
            },
        ),
        patch(
            "core.services.oauth_service.has_active_oauth",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("routers.container.dispatch_provision", new_callable=AsyncMock),
    ):
        result = await container_provision(auth=_auth())
    assert result["status"] == "provisioning"


@pytest.mark.asyncio
async def test_provision_existing_container_skips_payment_gate():
    """Idempotent return for users who already have a container should
    NOT re-run the gate — billing state can churn (cancel + resubscribe)
    and we don't want the existing container reported as 402."""
    from routers.container import container_provision

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"status": "running", "service_name": "openclaw-existing"},
        ),
    ):
        result = await container_provision(auth=_auth())
    assert result["already_existed"] is True
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_provision_reports_an_existing_workspace_without_re_provisioning_it():
    """Idempotent, and deliberately NOT payment-gated: billing state churns (cancel +
    resubscribe) and a stale 402 would mask the workspace the caller is asking about. There is no
    cold-start door left either -- AgentCore starts the microVM on the next invoke, so a caller
    with a row has nothing to ask us to start."""
    from routers import container as container_mod

    initialized = False

    async def initialize(_auth, owner_id):
        nonlocal initialized
        assert owner_id == "org_paid"
        initialized = True

    gate = AsyncMock()
    dispatch = AsyncMock()
    with (
        patch.object(container_mod, "ensure_workspace_trial_default", new=initialize),
        patch.object(
            container_mod.container_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"status": "stopped", "owner_id": "org_paid"}),
        ),
        patch.object(container_mod, "_assert_provision_allowed", gate),
        patch.object(container_mod, "dispatch_provision", dispatch),
    ):
        result = await container_mod.container_provision(auth=_auth())

    assert initialized is True
    assert result == {"status": "stopped", "owner_id": "org_paid", "already_existed": True}
    gate.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_402_returns_structured_blocked_payload():
    """Per provision-gate-ui spec: _assert_provision_allowed must raise
    402 with a structured `blocked` payload (not a free-form string)."""
    from core.services.provision_gate import Gate

    fake_gate = Gate(
        code="credits_required",
        title="Top up Claude credits to start your container",
        message="Top up some Claude credits to start your Bedrock container.",
        action_label="Top up now",
        action_href="/settings/billing#credits",
        action_admin_only=False,
        owner_role="admin",
    )

    with patch(
        "routers.container.evaluate_provision_gate",
        new_callable=AsyncMock,
    ) as mock_gate:
        mock_gate.return_value = fake_gate

        from routers.container import _assert_provision_allowed

        with pytest.raises(HTTPException) as exc:
            await _assert_provision_allowed(
                owner_id="user_x",
                is_admin=True,
            )

    assert exc.value.status_code == 402
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["blocked"]["code"] == "credits_required"
    assert exc.value.detail["blocked"]["action"]["href"] == "/settings/billing#credits"


@pytest.mark.asyncio
async def test_provision_402_subscription_required_payload():
    """Companion to credits_required test — pin the subscription_required
    path too, including the admin_only flag on the action.

    Existing test fixtures call _assert_provision_allowed / container_provision
    directly rather than going through an HTTP TestClient, so this matches
    that style for consistency with the rest of the file.
    """
    from core.services.provision_gate import Gate

    fake_gate = Gate(
        code="subscription_required",
        title="Subscribe to start your container",
        message="An active subscription is required to provision a container.",
        action_label="Subscribe",
        action_href="/onboarding",
        action_admin_only=True,
        owner_role="admin",
    )

    with patch(
        "routers.container.evaluate_provision_gate",
        new_callable=AsyncMock,
    ) as mock_gate:
        mock_gate.return_value = fake_gate

        from routers.container import _assert_provision_allowed

        with pytest.raises(HTTPException) as exc:
            await _assert_provision_allowed(
                owner_id="user_x",
                is_admin=True,
            )

    assert exc.value.status_code == 402
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["blocked"]["code"] == "subscription_required"
    assert exc.value.detail["blocked"]["action"]["admin_only"] is True
    assert exc.value.detail["blocked"]["action"]["href"] == "/onboarding"
