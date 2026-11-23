"""Tests for admin_service.list_users: a person is their own tenant, so the container and billing rows
are read by the Clerk user id."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.mark.asyncio
async def test_list_users_looks_up_container_by_user_id():
    """The container lookup key is the user id."""
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_solo", "email_addresses": [{"email_address": "solo@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    container_mock = AsyncMock(return_value={"status": "running"})
    billing_mock = AsyncMock(return_value={"subscription_status": "active"})

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    container_mock.assert_awaited_once_with("user_solo")
    billing_mock.assert_awaited_once_with("user_solo")
    assert len(result["users"]) == 1
    assert result["users"][0]["container_status"] == "running"
    assert result["users"][0]["subscription_status"] == "active"
    assert "org" not in result["users"][0]


@pytest.mark.asyncio
async def test_list_users_reads_subscription_status_from_billing_not_container():
    """Regression: plan_tier lives on the billing_accounts row (see
    billing_repo.put_billing), NOT on the container row. Reading it from the
    container row causes every user in the list view to render as "free"
    tier regardless of actual subscription, because container rows never
    carry a plan_tier field.
    """
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_paying", "email_addresses": [{"email_address": "pay@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    # Container row is deliberately WITHOUT plan_tier — which matches reality,
    # since plan_tier is not a field on the containers table.
    container_mock = AsyncMock(return_value={"status": "running", "service_name": "openclaw-x"})
    # Billing row is the authoritative source for plan_tier.
    billing_mock = AsyncMock(return_value={"subscription_status": "active"})

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    assert len(result["users"]) == 1
    assert result["users"][0]["subscription_status"] == "active", (
        "plan_tier must come from billing_repo, not container_repo; "
        "reading from containers returns 'free' because containers never have plan_tier"
    )


@pytest.mark.asyncio
async def test_list_users_subscription_status_is_none_when_no_billing_row():
    """Users with no billing row (never subscribed, pre-provisioning, etc.)
    should render as 'free' — the sensible default. Guards against the fix
    regressing in the opposite direction (KeyError on missing billing row).
    """
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_new", "email_addresses": [{"email_address": "new@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    container_mock = AsyncMock(return_value=None)
    billing_mock = AsyncMock(return_value=None)  # no billing row

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    assert result["users"][0]["subscription_status"] is None
    assert result["users"][0]["container_status"] == "none"
