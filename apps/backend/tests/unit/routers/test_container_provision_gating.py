"""Tests for /container/provision payment gating (C1 audit fix).

The primary `/container/provision` endpoint historically had no payment
check — any signed-in Clerk user could spin up an ECS Fargate task at
our cost. These tests pin down the gating contract:

  - No subscription on file              -> 402
  - subscription_status not in {active, trialing}
                                         -> 402
  - bedrock_claude with $0 balance       -> 402  (no point spinning a
                                                   container the user
                                                   can't chat through)
  - bedrock_claude with > 0 balance      -> 200
  - chatgpt_oauth / byo_key (any balance) -> 200
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _auth(user_id: str = "user_paid", org_id: str | None = None) -> MagicMock:
    auth = MagicMock()
    auth.user_id = user_id
    auth.org_id = org_id
    return auth


@pytest.mark.asyncio
async def test_provision_requires_subscription_row():
    """No billing row at all -> 402 Payment Required."""
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
            return_value=None,
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await container_provision(auth=_auth())
    assert exc.value.status_code == 402


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
    """bedrock_claude provider + $0 balance -> 402.

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
                "owner_id": "user_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "trialing",
                # Workstream B: provider_choice lives on billing_accounts now.
                "provider_choice": "bedrock_claude",
            },
        ),
        patch(
            "core.services.credit_ledger.get_balance",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await container_provision(auth=_auth())
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_provision_allows_bedrock_with_positive_balance():
    """bedrock_claude with credits topped up -> provisions normally."""
    from routers.container import container_provision

    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="openclaw-foo")

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
                "owner_id": "user_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "trialing",
                # Workstream B: provider_choice lives on billing_accounts now.
                "provider_choice": "bedrock_claude",
            },
        ),
        patch(
            "core.services.credit_ledger.get_balance",
            new_callable=AsyncMock,
            return_value=10_000_000,
        ),
        patch("routers.container.get_ecs_manager", return_value=fake_ecs),
    ):
        result = await container_provision(auth=_auth())
    assert result["status"] == "provisioning"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_choice", ["chatgpt_oauth", "byo_key"])
async def test_provision_skips_balance_check_for_non_bedrock(provider_choice):
    """chatgpt_oauth / byo_key never need a credit balance — their LLM
    costs land on the user's own provider account."""
    from routers.container import container_provision

    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="openclaw-foo")

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
                "owner_id": "user_paid",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "active",
                # Workstream B: provider_choice lives on billing_accounts now.
                "provider_choice": provider_choice,
                # The byo_provider sub-choice gate (Finding D on PR #563)
                # blocks byo_key without a sub-provider; this test isolates
                # the credit-balance behaviour, so seed a complete BYO row.
                "byo_provider": "anthropic" if provider_choice == "byo_key" else None,
            },
        ),
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new_callable=AsyncMock,
            return_value={"access_token": "x"},
        ),
        patch("routers.container.get_ecs_manager", return_value=fake_ecs),
    ):
        result = await container_provision(auth=_auth())
    assert result["status"] == "provisioning"


@pytest.mark.asyncio
async def test_provision_allows_legacy_row_with_subscription_id_but_no_status():
    """Codex P1 on PR #488: legacy billing rows pre-Plan-3 may have a
    valid stripe_subscription_id but no subscription_status backfilled
    yet. Other gates (gate_chat) preserve those — the provision gate
    must too, or we 402 paying customers mid-migration.

    bedrock_claude users still need credits regardless of legacy
    status, so this test uses chatgpt_oauth to isolate the legacy-row
    behavior.
    """
    from routers.container import container_provision

    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="openclaw-foo")

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
                "owner_id": "user_paid",
                "stripe_subscription_id": "sub_legacy_pre_plan_3",
                # subscription_status deliberately absent / None
                # Workstream B: provider_choice lives on billing_accounts now.
                "provider_choice": "chatgpt_oauth",
            },
        ),
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new_callable=AsyncMock,
            return_value={"access_token": "x"},
        ),
        patch("routers.container.get_ecs_manager", return_value=fake_ecs),
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
                clerk_user_id="user_x",
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
                clerk_user_id="user_x",
                is_admin=True,
            )

    assert exc.value.status_code == 402
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["blocked"]["code"] == "subscription_required"
    assert exc.value.detail["blocked"]["action"]["admin_only"] is True
    assert exc.value.detail["blocked"]["action"]["href"] == "/onboarding"
