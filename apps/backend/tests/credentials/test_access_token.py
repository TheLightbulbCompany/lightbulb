"""Workspace-owned OAuth access tokens for the Hermes credential broker."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.fixture
def stores(monkeypatch):
    from core.repositories import integrations_repo
    from core.services.credentials import integration_secret_store, oauth_dance
    from core.services import owner_lifecycle

    row = {"status": "connected"}
    credential = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "token_expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "client_id": "sentry-client",
        "client_secret": "",
    }
    saved = []

    @asynccontextmanager
    async def guard(owner_id):
        assert owner_id == "org_1"
        yield

    async def get_row(owner_id, provider):
        assert (owner_id, provider) == ("org_1", "sentry")
        return row

    async def set_status(owner_id, provider, status):
        assert (owner_id, provider) == ("org_1", "sentry")
        row["status"] = status

    async def get_secret(owner_id, provider):
        assert (owner_id, provider) == ("org_1", "sentry")
        return credential

    async def put_secret(owner_id, provider, value):
        saved.append(value.copy())
        credential.update(value)

    monkeypatch.setattr(owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(integrations_repo, "get", get_row)
    monkeypatch.setattr(integrations_repo, "set_status", set_status, raising=False)
    monkeypatch.setattr(integration_secret_store, "get", get_secret)
    monkeypatch.setattr(integration_secret_store, "put", put_secret)
    monkeypatch.setattr(oauth_dance, "_oauth_client", AsyncMock(return_value=("sentry-client", "")))
    return row, credential, saved


@pytest.mark.asyncio
async def test_disconnected_workspace_cannot_get_token(stores):
    from core.services.credentials import access_token

    row, _, saved = stores
    row["status"] = "disconnected"
    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_1", "sentry")
    assert saved == []


@pytest.mark.asyncio
async def test_fresh_grant_does_not_refresh(stores, monkeypatch):
    from core.services.credentials import access_token

    post = AsyncMock(side_effect=AssertionError("fresh grant should not call the provider"))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    assert await access_token.get("org_1", "sentry") == "old-access"
    assert stores[2] == []


@pytest.mark.asyncio
async def test_expired_grant_saves_rotated_refresh_token_before_issuance(stores, monkeypatch):
    from core.services.credentials import access_token

    _, credential, saved = stores
    credential["token_expiry"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    requests = []

    async def post(self, url, **kwargs):
        requests.append((url, kwargs["data"].copy()))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    assert await access_token.get("org_1", "sentry") == "new-access"
    assert requests == [
        (
            "https://mcp.sentry.dev/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": "sentry-client",
                "resource": "https://mcp.sentry.dev/mcp",
            },
        )
    ]
    assert len(saved) == 1
    assert saved[0]["refresh_token"] == "new-refresh"
    assert saved[0]["access_token"] == "new-access"


@pytest.mark.asyncio
async def test_rejected_refresh_does_not_replace_stored_grant(stores, monkeypatch):
    from core.services.credentials import access_token

    _, credential, saved = stores
    credential["token_expiry"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async def post(self, url, **kwargs):
        return httpx.Response(400, request=httpx.Request("POST", url), json={"error": "invalid_grant"})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_1", "sentry")
    assert saved == []
    assert credential["refresh_token"] == "old-refresh"
    assert stores[0]["status"] == "reconnect_required"


@pytest.mark.asyncio
async def test_changed_oauth_client_requires_reconnect_without_replacing_grant(stores, monkeypatch):
    from core.services.credentials import access_token, oauth_dance

    _, credential, saved = stores
    credential["token_expiry"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(oauth_dance, "_oauth_client", AsyncMock(return_value=("new-client", "")))

    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_1", "sentry")
    assert saved == []


@pytest.mark.asyncio
async def test_malformed_successful_refresh_never_replaces_grant(stores, monkeypatch):
    from core.services.credentials import access_token

    row, credential, saved = stores
    credential["token_expiry"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"error": "invalid_grant"})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_1", "sentry")
    assert saved == []
    assert row["status"] == "reconnect_required"
