"""Membership webhooks trigger the seat-quantity reconciler."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from routers import webhooks  # noqa: E402


@pytest.mark.asyncio
async def test_membership_created_calls_seat_sync(monkeypatch):
    monkeypatch.setattr(webhooks.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    sync = AsyncMock(return_value=3)
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        await webhooks._handle_organization_membership_created(
            {
                "organization": {"id": "org_1", "created_by": "user_owner"},
                "public_user_data": {"user_id": "user_new", "identifier": "new@example.com"},
            }
        )
    sync.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_membership_deleted_calls_seat_sync(monkeypatch):
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    sync = AsyncMock(return_value=2)
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        await webhooks._handle_organization_membership_deleted(
            {
                "organization": {"id": "org_1"},
                "public_user_data": {"user_id": "user_gone"},
            }
        )
    sync.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_seat_sync_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(webhooks.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    sync = AsyncMock(side_effect=RuntimeError("stripe down"))
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        # Must not raise — webhook has to keep returning 200.
        await webhooks._handle_organization_membership_created(
            {
                "organization": {"id": "org_1", "created_by": "user_owner"},
                "public_user_data": {"user_id": "user_new", "identifier": "new@example.com"},
            }
        )
