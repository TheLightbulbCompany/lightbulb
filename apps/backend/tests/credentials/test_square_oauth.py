"""Square's JSON OAuth contract, backend grant custody and hidden provider gate."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core.services.credentials import access_token, apply_connection, oauth_dance, registry

WS = "org_square_business"
REDIRECT = "https://api.example.test/api/v1/connections/callback"
REC = {"owner_id": WS, "provider": "square", "client_id": "square-app", "verifier": "unused-verifier"}


@pytest.fixture
def oauth(monkeypatch):
    original_client = httpx.AsyncClient
    state = {
        "requests": [],
        "payload": {
            "access_token": "mock-access",
            "refresh_token": "mock-refresh",
            "token_type": "bearer",
            "merchant_id": "merchant_1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        },
    }

    async def handle(request):
        assert request.method == "POST" and str(request.url) == "https://connect.squareup.com/oauth2/token"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Square-Version"] == "2026-09-16"
        assert "Authorization" not in request.headers
        state["requests"].append(request)
        return httpx.Response(state.get("status", 200), json=state["payload"])

    platform = AsyncMock(return_value={"client_id": "square-app", "client_secret": "mock-platform-secret"})
    byo = AsyncMock(side_effect=AssertionError("Square must not consult the per-owner app store"))
    monkeypatch.setattr(oauth_dance.platform_oauth_app_store, "get", platform)
    monkeypatch.setattr(oauth_dance.oauth_app_store, "get", byo)
    monkeypatch.setattr(
        oauth_dance.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw)
    )
    state["platform"], state["byo"] = platform, byo
    return state


def test_provider_backend_only_and_hidden_until_integration_acceptance():
    provider = registry.get_provider("square")
    assert provider.acquisition == "direct_oauth" and provider.refresh_owner == "backend"
    assert provider.oauth.platform_app == "square" and not provider.oauth.pkce
    assert provider.wiring.backend_only and apply_connection.runtime_supported(provider)
    assert not apply_connection.is_mcp_shaped(provider)
    assert not registry.available_for(provider, WS) and not registry.is_listable(provider, WS)
    assert set(provider.oauth.scopes) == {
        "APPOINTMENTS_READ",
        "APPOINTMENTS_ALL_READ",
        "APPOINTMENTS_WRITE",
        "APPOINTMENTS_ALL_WRITE",
        "APPOINTMENTS_BUSINESS_SETTINGS_READ",
        "MERCHANT_PROFILE_READ",
        "CUSTOMERS_READ",
    }


async def test_authorization_binds_state_and_uses_confidential_code_flow(oauth, monkeypatch):
    put = AsyncMock()
    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", put)
    url = await oauth_dance.start(WS, "square", redirect_uri=REDIRECT)
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/oauth2/authorize"
    assert query["client_id"] == ["square-app"] and query["session"] == ["false"]
    assert query["scope"][0].split() == registry.get_provider("square").oauth.scopes
    assert "code_challenge" not in query and "code_challenge_method" not in query
    assert put.await_args.kwargs["owner_id"] == WS
    assert put.await_args.kwargs["client_id"] == "square-app"
    assert put.await_args.args[0] == query["state"][0]
    assert not oauth["requests"]


async def exchange():
    return await oauth_dance.exchange(REC, "mock-code", redirect_uri=REDIRECT)


async def test_exchange_json_absolute_expiry_merchant_and_no_platform_secret_in_envelope(oauth):
    ws, provider, credential, _ = await exchange()
    assert (ws, provider) == (WS, "square")
    assert json.loads(oauth["requests"][0].content) == {
        "grant_type": "authorization_code",
        "code": "mock-code",
        "client_id": "square-app",
        "client_secret": "mock-platform-secret",
        "redirect_uri": REDIRECT,
    }
    assert credential["merchant_id"] == "merchant_1"
    assert credential["token_expiry"] == oauth["payload"]["expires_at"]
    assert credential["expires_in"] is None and access_token._expires_at(credential) > 0
    assert credential["client_secret"] == "" and credential["client_information"] == {"client_id": "square-app"}
    assert "mock-platform-secret" not in json.dumps(credential)
    oauth["byo"].assert_not_awaited()


async def test_refresh_json_preserves_merchant_and_replaces_absolute_expiry(oauth):
    credential = (await exchange())[2]
    oauth["payload"].update(access_token="mock-new-access", expires_at="2030-01-01T00:00:00Z")
    updated = await oauth_dance.refresh(WS, "square", credential)
    assert json.loads(oauth["requests"][-1].content) == {
        "grant_type": "refresh_token",
        "refresh_token": "mock-refresh",
        "client_id": "square-app",
        "client_secret": "mock-platform-secret",
    }
    assert updated["token_expiry"] == "2030-01-01T00:00:00+00:00"
    assert updated["merchant_id"] == "merchant_1" and updated["access_token"] == "mock-new-access"
    assert updated["refresh_token"] == "mock-refresh" and updated["client_secret"] == ""
    assert credential["access_token"] == "mock-access"  # no in-place mutation


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_at", None),
        ("expires_at", ""),
        ("expires_at", "2030-01-01T00:00:00"),
        ("expires_at", "2020-01-01T00:00:00Z"),
        ("merchant_id", ""),
        ("merchant_id", None),
        ("access_token", ""),
        ("refresh_token", None),
    ],
)
async def test_invalid_exchange_does_not_produce_usable_credential(oauth, field, value):
    oauth["payload"][field] = value
    with pytest.raises(ValueError, match="invalid token expiry or merchant"):
        await exchange()


async def test_refresh_cannot_rebind_existing_grant_to_another_merchant(oauth):
    credential = (await exchange())[2]
    oauth["payload"]["merchant_id"] = "merchant_2"
    with pytest.raises(ValueError, match="merchant binding"):
        await oauth_dance.refresh(WS, "square", credential)
    assert credential["merchant_id"] == "merchant_1"


async def test_changed_oauth_app_is_refused_before_exchange_or_refresh(oauth):
    credential = (await exchange())[2]
    oauth["platform"].return_value = {"client_id": "replacement-app", "client_secret": "other-secret"}
    with pytest.raises(ValueError):
        await exchange()
    with pytest.raises(ValueError):
        await oauth_dance.refresh(WS, "square", credential)
    assert len(oauth["requests"]) == 1


async def test_access_token_uses_existing_owner_lease_refresh_and_envelope_write(oauth, monkeypatch):
    credential = (await exchange())[2]
    credential["token_expiry"] = "2020-01-01T00:00:00+00:00"
    oauth["payload"].update(access_token="mock-fresh-access", expires_at="2030-01-01T00:00:00Z")
    leases = []

    @asynccontextmanager
    async def guard(ws):
        leases.append(ws)
        yield

    row = AsyncMock(return_value={"status": "connected"})
    get = AsyncMock(return_value=credential)
    put = AsyncMock()
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(access_token.integrations_repo, "get", row)
    monkeypatch.setattr(access_token.integration_secret_store, "get", get)
    monkeypatch.setattr(access_token.integration_secret_store, "put", put)
    assert await access_token.get(WS, "square") == "mock-fresh-access"
    assert leases == [WS]
    get.assert_awaited_once_with(WS, "square")
    stored = put.await_args.args
    assert stored[:2] == (WS, "square")
    assert stored[2]["merchant_id"] == "merchant_1" and stored[2]["token_expiry"] == "2030-01-01T00:00:00+00:00"


async def test_disconnected_grant_is_not_read_or_refreshed(oauth, monkeypatch):
    @asynccontextmanager
    async def guard(ws):
        yield

    get = AsyncMock()
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(access_token.integration_secret_store, "get", get)
    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get(WS, "square")
    get.assert_not_awaited()
    assert not oauth["requests"]


async def test_backend_only_connection_projects_no_credential_to_runtime(oauth, monkeypatch):
    projection = AsyncMock(side_effect=AssertionError("must not project"))
    monkeypatch.setattr(apply_connection, "_apply_connection_agentcore", projection)
    assert await apply_connection.apply_connection(WS, "square", (await exchange())[2]) is None
    projection.assert_not_awaited()
