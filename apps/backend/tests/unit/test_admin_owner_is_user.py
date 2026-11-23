"""The admin dashboard's owner is the Clerk user: a person is their own tenant, so every read and
mutation targets the user id from the URL with no lookup."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def admin_env(app):
    """Caller is on PLATFORM_ADMIN_USER_IDS — require_platform_admin admits them."""
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_platform_admin", email="admin@thelightbulb.company", human_session=True
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_get_overview_reads_the_rows_keyed_by_the_user_id():
    from core.services import admin_service

    with (
        patch("core.services.admin_service.clerk_admin.get_user", new=AsyncMock(return_value={"id": "user_solo"})),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ) as mock_container,
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)) as billing,
        patch("core.services.admin_service.usage_repo.get_period_usage", new=AsyncMock(return_value={})) as usage,
    ):
        result = await admin_service.get_overview("user_solo")

    mock_container.assert_awaited_once_with("user_solo")
    billing.assert_awaited_once_with("user_solo")
    assert usage.await_args.args[0] == "user_solo"
    assert "org" not in result


@pytest.mark.asyncio
async def test_container_reprovision_dispatches_on_the_user_id(async_client, admin_env):
    with patch("routers.admin.dispatch_reconcile", new_callable=AsyncMock) as dispatch:
        res = await async_client.post("/api/v1/admin/users/user_123/container/reprovision")
    assert res.status_code == 200, res.text
    assert res.json()["owner_id"] == "user_123"
    dispatch.assert_awaited_once_with("user_123")


@pytest.mark.asyncio
async def test_account_suspend_bans_the_user_id(async_client, admin_env):
    with patch("routers.admin.clerk_admin.ban_user", new=AsyncMock(return_value={"banned": True})) as mock_ban:
        res = await async_client.post("/api/v1/admin/users/user_123/account/suspend")
    assert res.status_code == 200
    mock_ban.assert_awaited_once_with("user_123")
