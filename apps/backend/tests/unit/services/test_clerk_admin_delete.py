"""Unit tests for clerk_admin.delete_user — Admin Account-Wipe Task 2.

Same direct-httpx-mock approach as test_clerk_admin_invitations.py (patch
core.services.clerk_admin.httpx.AsyncClient with an AsyncMock supporting the
async-context-manager protocol).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services import clerk_admin


class _MockResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_delete_user_issues_delete_to_correct_url_with_bearer_auth():
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.delete.return_value = _MockResp(200)
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.delete_user("user_abc")

    assert result is None
    mock_client.delete.assert_awaited_once()
    args, kwargs = mock_client.delete.call_args
    assert args[0] == "https://api.clerk.com/v1/users/user_abc"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_123"


@pytest.mark.asyncio
async def test_delete_user_swallows_404_as_success():
    """Already-deleted (or never-existed) user — a re-run after a partial
    wipe failure must not raise."""
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.delete.return_value = _MockResp(404)
        mock_client_cls.return_value = mock_client

        result = await clerk_admin.delete_user("user_gone")

    assert result is None


@pytest.mark.asyncio
async def test_delete_user_raises_on_other_http_error():
    """Unlike this module's read-side helpers (which degrade to an {"error"}
    dict), delete_user must RAISE on real failures — the wipe orchestrator
    relies on the exception to mark the step as failed rather than silently
    treating it as done."""
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.delete.return_value = _MockResp(500, text="internal error")
        mock_client_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="http_500"):
            await clerk_admin.delete_user("user_abc")


@pytest.mark.asyncio
async def test_delete_user_raises_on_network_error():
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.delete.side_effect = httpx.TimeoutException("slow")
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.TimeoutException):
            await clerk_admin.delete_user("user_abc")


@pytest.mark.asyncio
async def test_delete_user_noop_when_no_secret_key():
    with patch.object(clerk_admin, "settings") as mock_settings:
        mock_settings.CLERK_SECRET_KEY = ""
        result = await clerk_admin.delete_user("user_abc")

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_list_user_organizations_strict_raises_on_http_error(strict):
    """The workspace wipe reads "no other orgs" as "delete the account", so its
    strict read must fail closed; the default keeps the degrade-to-[] posture."""
    with (
        patch.object(clerk_admin, "settings") as mock_settings,
        patch("core.services.clerk_admin.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.CLERK_SECRET_KEY = "sk_test_123"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get.return_value = _MockResp(503)
        mock_client_cls.return_value = mock_client

        if strict:
            with pytest.raises(RuntimeError, match="http_503"):
                await clerk_admin.list_user_organizations("user_x", strict=True)
        else:
            assert await clerk_admin.list_user_organizations("user_x") == []
