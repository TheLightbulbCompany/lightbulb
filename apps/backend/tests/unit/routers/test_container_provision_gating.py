"""Tests for /container/provision and /container/retry: not billing-gated (voice).

A workspace on the shared Hermes pool costs nothing until it runs, so both doors initialize the
trial row and dispatch whatever the billing row says. Chat, the orb and the phone gate their own
spend. ``_assert_provision_allowed`` survives for the browser's start-on-view door.
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
    # A bare MagicMock auto-creates ``is_org_context``/``is_org_admin`` as truthy mocks; set them
    # to match the real ``AuthContext`` property semantics.
    auth.is_org_context = org_id is not None
    auth.is_org_admin = True
    return auth


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "billing",
    [
        {"stripe_subscription_id": "sub_123", "subscription_status": "canceled"},
        {"stripe_subscription_id": "sub_123", "subscription_status": "past_due"},
        {"subscription_status": "trial_local", "trial_end": 1},
        {"subscription_status": "active", "provider_choice": "openrouter_managed"},
    ],
)
async def test_provision_dispatches_without_a_billing_gate(billing):
    """Canceled, past due, an expired trial, zero credits: the workspace is still provisioned,
    and the gate is never read."""
    from routers import container as container_mod

    initialized = []
    dispatch = AsyncMock()
    gate = AsyncMock()
    with (
        patch.object(
            container_mod,
            "ensure_workspace_trial_default",
            new=AsyncMock(side_effect=lambda _a, o: initialized.append(o)),
        ),
        patch.object(container_mod.container_repo, "get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.billing_repo.get_by_owner_id", new=AsyncMock(return_value=billing)),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
        patch.object(container_mod, "evaluate_provision_gate", gate),
        patch.object(container_mod, "dispatch_provision", dispatch),
    ):
        result = await container_mod.container_provision(auth=_auth())

    assert initialized, "the trial row is initialized first"
    dispatch.assert_awaited_once_with("org_paid")
    gate.assert_not_awaited()
    assert result["already_existed"] is False


@pytest.mark.asyncio
async def test_retry_dispatches_an_errored_workspace_without_a_billing_gate():
    from routers import container as container_mod

    dispatch = AsyncMock()
    gate = AsyncMock()
    with (
        patch.object(container_mod, "ensure_workspace_trial_default", new=AsyncMock()),
        patch.object(container_mod, "evaluate_provision_gate", gate),
        patch.object(container_mod, "dispatch_provision", dispatch),
        patch.object(
            container_mod.container_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "org_paid", "status": "error"}),
        ),
    ):
        assert await container_mod.container_retry(auth=_auth()) == {"ok": True}
    dispatch.assert_awaited_once_with("org_paid")
    gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_refuses_a_running_workspace():
    from routers import container as container_mod

    with (
        patch.object(container_mod, "ensure_workspace_trial_default", new=AsyncMock()),
        patch.object(
            container_mod.container_repo, "get_by_owner_id", new=AsyncMock(return_value={"status": "running"})
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await container_mod.container_retry(auth=_auth())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_provision_reports_an_existing_workspace_without_re_provisioning_it():
    """Idempotent: an existing workspace is reported, never re-dispatched. There is no cold-start
    door either -- AgentCore starts the microVM on the next invoke."""
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
        patch.object(container_mod, "evaluate_provision_gate", gate),
        patch.object(container_mod, "dispatch_provision", dispatch),
    ):
        result = await container_mod.container_provision(auth=_auth())

    assert initialized is True
    assert result == {"status": "stopped", "owner_id": "org_paid", "already_existed": True}
    gate.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_402_returns_structured_blocked_payload():
    """_assert_provision_allowed (the browser start-on-view door) raises 402 with a structured
    `blocked` payload, not a free-form string."""
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
