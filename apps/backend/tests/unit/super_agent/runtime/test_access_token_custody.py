"""Spec 13 T6: X, QuickBooks and Instagram under backend custody (row ``custody`` wins)."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services.credentials import access_token, oauth_dance, registry

_REAL_REFRESH = oauth_dance.refresh  # the fixture replaces it on the shared module


def _in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def wired(monkeypatch):
    leases: list[str] = []

    @asynccontextmanager
    async def _lease(owner_id):
        leases.append(owner_id)
        yield

    state = {"row": {"status": "connected", "custody": "backend"}, "cred": {}}
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _lease)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(side_effect=lambda *a, **k: state["row"]))
    monkeypatch.setattr(
        access_token.integration_secret_store, "get", AsyncMock(side_effect=lambda *a, **k: dict(state["cred"]))
    )
    put = AsyncMock()
    monkeypatch.setattr(access_token.integration_secret_store, "put", put)
    refresh = AsyncMock(side_effect=lambda o, p, c: {**c, "access_token": "AT-new", "token_expiry": _in(7200)})
    monkeypatch.setattr(access_token.oauth_dance, "refresh", refresh)
    return {"leases": leases, "state": state, "put": put, "refresh": refresh}


def test_refresh_window_per_provider():
    windows = {k: registry.get_provider(k).refresh_window_s for k in ("x", "quickbooks", "instagram", "stripe")}
    assert windows == {"x": 600, "quickbooks": 600, "instagram": 2_592_000, "stripe": 60}
    assert {k for k, p in registry.all_providers().items() if p.custody_migratable} == {"x", "quickbooks", "instagram"}


def test_row_custody_overrides_refresh_owner():
    x = registry.get_provider("x")
    assert registry.refresh_owner_for(x) == "actor"
    assert registry.refresh_owner_for(x, None) == "actor"
    assert registry.refresh_owner_for(x, {"status": "connected"}) == "actor"
    assert registry.refresh_owner_for(x, {"custody": "backend"}) == "backend"
    stripe = registry.get_provider("stripe")
    assert registry.refresh_owner_for(stripe, {"custody": "actor"}) == "actor"


@pytest.mark.asyncio
async def test_fast_path_no_lease_outside_window(wired):
    wired["state"]["cred"] = {"access_token": "AT-1", "refresh_token": "RT", "token_expiry": _in(900)}
    token, expires_at = await access_token.get_with_expiry("org_a", "x")
    assert token == "AT-1"
    assert abs(expires_at - (datetime.now(timezone.utc).timestamp() + 900)) < 5
    assert wired["leases"] == [] and wired["refresh"].await_count == 0

    # Inside X's 600 s window: lease, refresh, persist.
    wired["state"]["cred"] = {"access_token": "AT-1", "refresh_token": "RT", "token_expiry": _in(300)}
    token, _ = await access_token.get_with_expiry("org_a", "x")
    assert token == "AT-new" and wired["leases"] == ["org_a"]
    wired["put"].assert_awaited_once()
    assert await access_token.get("org_a", "x") == "AT-new"  # ``get`` stays the plain-token wrapper


@pytest.mark.asyncio
async def test_legacy_rows_unchanged(wired):
    # No custody on the row: X is actor-refreshed, as before -- the backend refuses to vend it.
    wired["state"]["row"] = {"status": "connected"}
    with pytest.raises(ValueError):
        await access_token.get("org_a", "x")


@pytest.mark.asyncio
async def test_instagram_window_30_days(wired):
    wired["state"]["cred"] = {"access_token": "IG-1", "token_expiry": _in(31 * 86400)}
    assert (await access_token.get_with_expiry("org_a", "instagram"))[0] == "IG-1"
    assert wired["leases"] == []


@pytest.mark.asyncio
async def test_ig_refresh_branch(wired, monkeypatch):
    # (respx 0.20 cannot match a GET carrying params under this httpx; capture the call instead.)
    monkeypatch.setattr(access_token.oauth_dance, "refresh", _REAL_REFRESH)
    wired["state"]["cred"] = {"access_token": "IG-1", "token_expiry": _in(10 * 86400)}
    sent: list[httpx.Request] = []

    async def fake_get(self, url, *, params=None, headers=None, **kw):
        request = httpx.Request("GET", url, params=params, headers=headers)
        sent.append(request)
        body = {"access_token": "IG-2", "token_type": "bearer", "expires_in": 5184000}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token, expires_at = await access_token.get_with_expiry("org_a", "instagram")
    assert token == "IG-2"
    assert expires_at > datetime.now(timezone.utc).timestamp() + 59 * 86400
    [request] = sent
    assert request.url.host == "graph.instagram.com" and request.url.path == "/refresh_access_token"
    assert "IG-1" not in str(request.url)
    assert request.url.params["grant_type"] == "ig_refresh_token"
    assert request.headers["Authorization"] == "Bearer IG-1"
    assert wired["put"].await_args.args[2]["access_token"] == "IG-2"
