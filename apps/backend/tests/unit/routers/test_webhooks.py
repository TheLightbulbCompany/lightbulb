"""Unit tests for organization-membership webhook record keeping."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_membership_created_renews_member_lifecycle():
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }

    with patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()) as renew:
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)

    renew.assert_awaited_once_with("org_xyz", "user_member", None)


@pytest.mark.asyncio
async def test_membership_created_ignores_missing_email():
    payload = {"organization": {"id": "org_xyz"}, "public_user_data": {"user_id": "user_member"}}

    with patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()) as renew:
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)

    renew.assert_not_awaited()
