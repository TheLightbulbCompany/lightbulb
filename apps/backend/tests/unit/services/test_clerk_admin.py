"""Tests for clerk_admin (Phase B v1 read-side surface).

Same direct-httpx-mock approach as test_posthog_admin.py — respx 0.20.2
+ newer httpx have a route-matching incompat.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _make_response(status_code: int, json_body=None):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body if json_body is not None else [])
    return response


@asynccontextmanager
async def _fake_client(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.get = AsyncMock(side_effect=response_or_exc)
    else:
        client.get = AsyncMock(return_value=response_or_exc)
    yield client


@pytest.mark.asyncio
async def test_list_users_stubs_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services.clerk_admin import list_users

    result = await list_users(query="x", limit=10, offset=0)
    assert result["stubbed"] is True
    assert result["users"] == []
    assert result["next_offset"] is None


@pytest.mark.asyncio
async def test_list_users_happy_path(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    body = [{"id": "user_a"}, {"id": "user_b"}]
    response = _make_response(200, json_body=body)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users(query="", limit=2, offset=0)

    assert len(result["users"]) == 2
    assert result["users"][0]["id"] == "user_a"
    assert result["next_offset"] == 2  # got `limit` results → there may be more
    assert result["stubbed"] is False


@pytest.mark.asyncio
async def test_list_users_no_more_pages_when_under_limit(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(200, json_body=[{"id": "user_a"}])
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users(limit=10)

    assert result["next_offset"] is None  # < limit → end of list


@pytest.mark.asyncio
async def test_list_users_timeout_returns_error(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(httpx.TimeoutException("slow")),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users()

    assert result["error"] == "timeout"
    assert result["users"] == []


@pytest.mark.asyncio
async def test_list_users_5xx_returns_error(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(503, json_body={})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users()

    assert result["error"] == "http_503"


@pytest.mark.asyncio
async def test_get_user_returns_none_on_404(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(404, json_body={})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import get_user

        result = await get_user("user_missing")

    assert result is None


@pytest.mark.asyncio
async def test_get_user_returns_none_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services.clerk_admin import get_user

    assert await get_user("any") is None


@pytest.mark.asyncio
async def test_get_user_returns_payload_on_200(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(200, json_body={"id": "user_a", "first_name": "Alice"})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import get_user

        result = await get_user("user_a")

    assert result["id"] == "user_a"
    assert result["first_name"] == "Alice"
