"""Unit tests for clerk_admin.list_pending_invitations_for_user.

Critical contract: it MUST return [] on every failure path so the gates
fail-open during Clerk outages. Changing this contract to raise would silently
break /trial-checkout in any Clerk outage.

The `create_organization_invitation` cases that used to live here went with the
helper, which was removed alongside the email-invitation endpoint (join links
replaced it). Clerk's own pending-invitation LIST is still read by billing Gate
B, which is why this file survives.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services import clerk_admin


@pytest.mark.asyncio
async def test_list_pending_invitations_returns_empty_when_no_secret_key():
    """Local dev without CLERK_SECRET_KEY: stub to []."""
    with patch.object(clerk_admin, "settings") as mock_settings:
        mock_settings.CLERK_SECRET_KEY = ""
        result = await clerk_admin.list_pending_invitations_for_user("user_x")
    assert result == []


@pytest.mark.asyncio
async def test_list_pending_invitations_returns_empty_on_network_error():
    """Network failures must NOT raise — returning [] keeps Gate B fail-open
    so a Clerk outage doesn't block all personal trial-checkouts."""
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.side_effect = httpx.TimeoutException("timeout")
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.list_pending_invitations_for_user("user_x")
    assert result == []


@pytest.mark.asyncio
async def test_list_pending_invitations_returns_empty_on_http_error():
    """4xx/5xx from Clerk must NOT raise — same fail-open contract."""

    class _MockResp:
        status_code = 503
        text = "Service Unavailable"

        def json(self):
            return {}

    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = _MockResp()
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.list_pending_invitations_for_user("user_x")
    assert result == []


@pytest.mark.asyncio
async def test_list_pending_invitations_returns_data_on_success():
    """Happy path: returns the data list from Clerk's paginated envelope."""

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "data": [
                    {"id": "orginv_1", "public_organization_data": {"name": "Acme"}},
                    {"id": "orginv_2"},
                ],
                "total_count": 2,
            }

    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = _MockResp()
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.list_pending_invitations_for_user("user_x")

    assert len(result) == 2
    assert result[0]["id"] == "orginv_1"


@pytest.mark.asyncio
async def test_list_pending_invitations_handles_bare_list_payload():
    """Some Clerk endpoints return bare lists, not envelopes — both shapes
    must work."""

    class _MockResp:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": "orginv_a"}, {"id": "orginv_b"}]

    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = _MockResp()
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.list_pending_invitations_for_user("user_x")

    assert len(result) == 2
    assert result[0]["id"] == "orginv_a"
