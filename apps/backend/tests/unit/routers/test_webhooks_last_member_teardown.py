"""The last member leaving a Clerk org tears the workspace down (the org itself stays in Clerk,
so organization.deleted never fires). And organization.deleted tears the RUNTIME down itself:
a free trial has no Stripe subscription whose cancellation would do it."""

from unittest.mock import AsyncMock

import pytest

from routers import webhooks


@pytest.fixture
def clerk_key(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "CLERK_SECRET_KEY", "sk_test")


@pytest.fixture
def org_deleted(monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr(webhooks, "_handle_organization_deleted", handler)
    return handler


def _members(monkeypatch, value):
    mock = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", mock)
    return mock


@pytest.mark.asyncio
async def test_an_org_with_no_members_left_is_torn_down(monkeypatch, clerk_key, org_deleted):
    members = _members(monkeypatch, [])
    await webhooks._teardown_if_no_members_left("org_a")
    members.assert_awaited_once_with("org_a", strict=True)
    org_deleted.assert_awaited_once_with({"id": "org_a"})


@pytest.mark.asyncio
async def test_an_org_with_members_left_is_not_touched(monkeypatch, clerk_key, org_deleted):
    _members(monkeypatch, [{"user_id": "user_b"}])
    await webhooks._teardown_if_no_members_left("org_a")
    org_deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_clerk_blip_fails_closed_instead_of_reading_as_empty(monkeypatch, clerk_key, org_deleted):
    _members(monkeypatch, RuntimeError("clerk_admin.list_organization_members failed: http_503"))
    with pytest.raises(RuntimeError):
        await webhooks._teardown_if_no_members_left("org_a")
    org_deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_clerk_key_is_unknown_not_empty(monkeypatch, org_deleted):
    monkeypatch.setattr(webhooks.settings, "CLERK_SECRET_KEY", "")
    members = _members(monkeypatch, [])
    await webhooks._teardown_if_no_members_left("org_a")
    members.assert_not_awaited()
    org_deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_organization_deleted_tears_the_runtime_down(monkeypatch):
    teardown = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.hermes_provisioning.delete_workspace_runtime", teardown)
    monkeypatch.setattr("core.services.workspace_join.revoke_join_link", AsyncMock())
    monkeypatch.setattr("core.repositories.openrouter_key_repo.mark_retiring", AsyncMock())
    monkeypatch.setattr(
        "core.services.billing_service.cancel_subscription_for_owner",
        AsyncMock(return_value={"status": "no_subscription"}),
    )
    disable = AsyncMock()
    monkeypatch.setattr("core.services.workspace_provisioning.disable_workspace", disable)

    await webhooks._handle_organization_deleted({"id": "org_a"})

    teardown.assert_awaited_once_with("org_a")
    disable.assert_awaited_once_with("org_a")
