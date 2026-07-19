import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import meta_token_refresh  # noqa: E402


def _iso(days_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).isoformat()


def test_is_stale_boundaries():
    assert meta_token_refresh._is_stale(_iso(5)) is True  # inside 30d window
    assert meta_token_refresh._is_stale(_iso(45)) is False  # fresh
    assert meta_token_refresh._is_stale("") is True  # unknown expiry → try
    assert meta_token_refresh._is_stale("not-a-date") is True


@pytest.mark.asyncio
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.get", new_callable=AsyncMock)
async def test_no_credential_is_noop(mock_get):
    mock_get.return_value = None
    assert await meta_token_refresh.refresh_instagram_if_stale("org_1") is False


@pytest.mark.asyncio
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.get", new_callable=AsyncMock)
async def test_fresh_token_is_noop(mock_get):
    mock_get.return_value = {"access_token": "T", "token_expiry": _iso(45)}
    assert await meta_token_refresh.refresh_instagram_if_stale("org_1") is False


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"access_token": "REFRESHED", "token_type": "bearer", "expires_in": 5184000}


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        return _FakeResponse()


@pytest.mark.asyncio
@patch("core.services.credentials.meta_token_refresh.httpx.AsyncClient", _FakeAsyncClient)
@patch("core.services.credentials.meta_token_refresh.apply_connection.apply_connection", new_callable=AsyncMock)
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.put", new_callable=AsyncMock)
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.get", new_callable=AsyncMock)
async def test_stale_token_refreshes_and_rematerializes(mock_get, mock_put, mock_apply):
    mock_get.return_value = {"access_token": "OLD", "token_expiry": _iso(5)}
    assert await meta_token_refresh.refresh_instagram_if_stale("org_1") is True
    stored = mock_put.await_args.args[2]
    assert stored["access_token"] == "REFRESHED"
    assert stored["expires_in"] == 5184000
    assert stored["token_expiry"]
    mock_apply.assert_awaited_once_with("org_1", "instagram", stored)


@pytest.mark.asyncio
@patch("core.services.credentials.meta_token_refresh.apply_connection.apply_connection", new_callable=AsyncMock)
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.put", new_callable=AsyncMock)
@patch("core.services.credentials.meta_token_refresh.integration_secret_store.get", new_callable=AsyncMock)
async def test_graph_failure_is_swallowed(mock_get, mock_put, mock_apply):
    mock_get.return_value = {"access_token": "OLD", "token_expiry": _iso(5)}
    failing = AsyncMock(side_effect=RuntimeError("graph down"))
    with patch("core.services.credentials.meta_token_refresh.httpx.AsyncClient", failing):
        assert await meta_token_refresh.refresh_instagram_if_stale("org_1") is False
    mock_put.assert_not_awaited()
    mock_apply.assert_not_awaited()
