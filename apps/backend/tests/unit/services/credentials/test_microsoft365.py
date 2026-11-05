"""Microsoft 365 (Graph): platform multi-tenant Entra app, backend-held refresh
grant, per-turn access token through the runtime credential broker."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import jwt  # noqa: E402
import pytest  # noqa: E402

from core.services.credentials import access_token, apply_connection, oauth_dance, registry  # noqa: E402


@pytest.fixture(autouse=True)
def _owner_lifecycle_lease(monkeypatch):
    """The callback and disconnect take the owner lease (a DynamoDB conditional write)."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(container_repo, "get_oauth_revoked_at", AsyncMock(return_value=None))


_ID_TOKEN = jwt.encode({"tid": "tenant-a", "preferred_username": "ada@contoso.com"}, "k", algorithm="HS256")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    last_post: dict = {}
    reply: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, headers=None, auth=None):
        _Client.last_post = {"url": url, "data": data}
        return _Resp(_Client.reply)


def test_registry_entry_is_platform_app_backend_custody_read_only():
    p = registry.get_provider("microsoft365")
    assert p.oauth.platform_app == "microsoft365"
    assert p.oauth.authorization_url == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert p.oauth.token_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert p.oauth.scopes == [
        "offline_access",
        "openid",
        "profile",
        "email",
        "User.Read",
        "Mail.Read",
        "Calendars.Read",
        "Files.Read.All",
    ]
    assert p.refresh_owner == "backend"
    assert p.wiring.backend_only
    assert apply_connection.runtime_supported(p)


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _Client)
@patch("core.services.credentials.oauth_dance.platform_oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance.oauth_state_repo.consume", new_callable=AsyncMock)
async def test_exchange_records_tenant_and_identity_and_blanks_platform_secret(mock_consume, mock_app):
    mock_consume.return_value = {"owner_id": "org_a", "provider": "microsoft365", "verifier": "v" * 43}
    mock_app.return_value = {"client_id": "entra-cid", "client_secret": "entra-secret"}
    _Client.reply = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "id_token": _ID_TOKEN}

    _, _, cred, _ = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st"), "code", redirect_uri="https://api/cb"
    )

    assert _Client.last_post["data"]["client_secret"] == "entra-secret"
    assert cred["tenant_id"] == "tenant-a"
    assert cred["account"] == "ada@contoso.com"
    assert cred["refresh_token"] == "RT"
    assert cred["client_secret"] == ""
    assert "id_token" not in cred


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _Client)
@patch("core.services.credentials.oauth_dance.platform_oauth_app_store.get", new_callable=AsyncMock)
async def test_refresh_persists_microsofts_rotated_refresh_token(mock_app):
    mock_app.return_value = {"client_id": "entra-cid", "client_secret": "entra-secret"}
    _Client.reply = {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600}
    old = {"access_token": "AT", "refresh_token": "RT", "client_id": "entra-cid", "client_secret": "", "tenant_id": "t"}

    new = await oauth_dance.refresh("org_a", "microsoft365", old)

    sent = _Client.last_post["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "RT"
    assert sent["client_secret"] == "entra-secret"
    assert "Files.Read.All" in sent["scope"]
    assert (new["access_token"], new["refresh_token"]) == ("AT2", "RT2")
    assert new["token_expiry"]
    assert new["client_secret"] == ""  # the platform secret never lands in the owner's credential
    assert new["tenant_id"] == "t"


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _Client)
@patch("core.services.credentials.oauth_dance.platform_oauth_app_store.get", new_callable=AsyncMock)
async def test_refresh_keeps_the_old_refresh_token_when_none_is_returned(mock_app):
    mock_app.return_value = {"client_id": "entra-cid", "client_secret": "entra-secret"}
    _Client.reply = {"access_token": "AT2", "expires_in": 3600}
    new = await oauth_dance.refresh("org_a", "microsoft365", {"access_token": "AT", "refresh_token": "RT"})
    assert new["refresh_token"] == "RT"


@asynccontextmanager
async def _no_lease(owner_id):
    yield


@pytest.mark.asyncio
async def test_access_token_refreshes_an_expired_grant_and_stores_the_rotation(monkeypatch):
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stored = {"access_token": "AT", "refresh_token": "RT", "token_expiry": expired}
    put = AsyncMock()
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _no_lease)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=stored))
    monkeypatch.setattr(access_token.integration_secret_store, "put", put)
    monkeypatch.setattr(
        access_token.oauth_dance, "refresh", AsyncMock(return_value={"access_token": "AT2", "refresh_token": "RT2"})
    )

    assert await access_token.get("org_a", "microsoft365") == "AT2"
    put.assert_awaited_once_with("org_a", "microsoft365", {"access_token": "AT2", "refresh_token": "RT2"})


@pytest.mark.asyncio
async def test_broker_vends_graph_token_only_to_the_owning_org(async_client, monkeypatch):
    from core.services import container_auth
    from routers import runtime_rpc

    containers = {"org_a": {"hook_token": "hook-a"}, "org_b": {"hook_token": "hook-b"}}
    rows = {"org_a": [{"provider": "microsoft365", "status": "connected"}], "org_b": []}
    monkeypatch.setattr(container_auth.container_repo, "get_by_owner_id", AsyncMock(side_effect=containers.get))
    monkeypatch.setattr(runtime_rpc.integrations_repo, "list_for_owner", AsyncMock(side_effect=rows.get))
    monkeypatch.setattr(runtime_rpc.messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))
    minted = AsyncMock(side_effect=lambda owner, provider: (f"graph-{owner}", 0))  # 0 = expiry unknown
    monkeypatch.setattr(runtime_rpc.access_token, "get_with_expiry", minted)

    def call(owner, hook):
        return async_client.post(
            f"/api/v1/runtime/credentials/u/{owner}",
            json={"agent_id": "ceo"},
            headers={"Authorization": f"Bearer {hook}"},
        )

    a = await call("org_a", "hook-a")
    assert a.json()["tokens"] == {"microsoft365": "graph-org_a"}
    # Org B has no grant of its own and cannot reach org A's.
    b = await call("org_b", "hook-b")
    assert b.json()["tokens"] == {}
    assert (await call("org_a", "hook-b")).status_code == 401
    minted.assert_awaited_once_with("org_a", "microsoft365")


@pytest.mark.asyncio
@patch("routers.connections.integration_secret_store")
@patch("routers.connections.integrations_repo")
@patch("routers.connections.oauth_dance")
@patch("routers.connections.oauth_state_repo.consume", new_callable=AsyncMock)
async def test_callback_retains_grant_and_records_tenant_and_identity(
    mock_consume, mock_dance, mock_repo, mock_store, async_client
):
    mock_consume.return_value = {"owner_id": "org_a", "provider": "microsoft365", "verifier": "pkce"}
    cred = {"access_token": "AT", "refresh_token": "RT", "tenant_id": "tenant-a", "account": "ada@contoso.com"}
    mock_dance.exchange = AsyncMock(return_value=("org_a", "microsoft365", cred, None))
    mock_store.put = AsyncMock()
    mock_repo.put_status = AsyncMock()

    response = await async_client.get("/api/v1/oauth/microsoft365/callback?code=c&state=s", follow_redirects=False)

    assert "connected=microsoft365" in response.headers["location"]
    mock_store.put.assert_awaited_once_with("org_a", "microsoft365", cred)
    mock_repo.put_status.assert_awaited_once_with(
        "org_a", "microsoft365", "connected", identity="ada@contoso.com", tenant_id="tenant-a"
    )


@pytest.mark.asyncio
@patch("routers.connections.integration_secret_store")
@patch("routers.connections.integrations_repo")
async def test_disconnect_deletes_the_secret_and_the_row(mock_repo, mock_store, async_client):
    mock_store.delete = AsyncMock()
    mock_repo.delete = AsyncMock()

    response = await async_client.delete("/api/v1/connections/microsoft365")

    assert response.status_code == 200
    mock_store.delete.assert_awaited_once_with("org_test_456", "microsoft365")
    mock_repo.delete.assert_awaited_once_with("org_test_456", "microsoft365")


def test_microsoft365_available_in_prod():
    # Both Entra apps and both platform secrets exist; the provider ships everywhere.
    assert registry.get_provider("microsoft365").environments is None


@pytest.mark.asyncio
async def test_a_connected_row_with_no_grant_flips_to_reconnect_required(monkeypatch):
    # Prod 2026-09-24: a pre-brokering Stripe connect left a connected row and no token secret;
    # this path called a repo function that did not exist and 500'd every turn's credential call.
    set_status = AsyncMock()
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _no_lease)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(access_token.integrations_repo, "set_status", set_status)
    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_a", "microsoft365")
    set_status.assert_awaited_once_with("org_a", "microsoft365", "reconnect_required", reason="no_access_token")


@pytest.mark.asyncio
async def test_one_providers_unexpected_error_never_fails_the_whole_broker_call(async_client, monkeypatch):
    from core.services import container_auth
    from routers import runtime_rpc

    monkeypatch.setattr(
        container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-a"})
    )
    monkeypatch.setattr(
        runtime_rpc.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "microsoft365", "status": "connected"}]),
    )
    monkeypatch.setattr(runtime_rpc.messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))
    monkeypatch.setattr(runtime_rpc.access_token, "get_with_expiry", AsyncMock(side_effect=AttributeError("boom")))
    r = await async_client.post(
        "/api/v1/runtime/credentials/u/org_a", json={"agent_id": "ceo"}, headers={"Authorization": "Bearer hook-a"}
    )
    assert r.status_code == 200
    assert r.json() == {"tokens": {}, "unavailable": ["microsoft365"]}
