"""Meta deauthorize + data-deletion routes.

These are UNAUTHENTICATED — the signed_request signature is the only gate — so
the load-bearing assertion in most of these tests is that nothing was torn down.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers import meta_account_callbacks


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(meta_account_callbacks.router, prefix="/api/v1/webhooks/meta")
    return a


@pytest.fixture
def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def torn_down(monkeypatch):
    """Record teardown calls so every test can assert on them."""
    calls: list[tuple] = []

    async def _fake(provider, user_id):
        calls.append((provider, user_id))
        return "org_1"

    monkeypatch.setattr(meta_account_callbacks, "_teardown", _fake)
    return calls


def _verified(monkeypatch, payload: dict):
    monkeypatch.setattr(meta_account_callbacks.meta_signed_request, "verify", AsyncMock(return_value=payload))


def _rejects(monkeypatch):
    async def _boom(_sr, _app):
        raise meta_account_callbacks.SignatureError("nope")

    monkeypatch.setattr(meta_account_callbacks.meta_signed_request, "verify", _boom)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["deauthorize", "data-deletion"])
async def test_bad_signature_is_400_and_tears_down_nothing(client, monkeypatch, torn_down, route):
    _rejects(monkeypatch)
    resp = await client.post(f"/api/v1/webhooks/meta/instagram/{route}", data={"signed_request": "forged.payload"})
    assert resp.status_code == 400
    assert torn_down == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["deauthorize", "data-deletion"])
async def test_missing_signed_request_is_400(client, monkeypatch, torn_down, route):
    _rejects(monkeypatch)
    resp = await client.post(f"/api/v1/webhooks/meta/instagram/{route}", data={})
    assert resp.status_code == 400
    assert torn_down == []


@pytest.mark.asyncio
async def test_valid_deauthorize_tears_down_that_user(client, monkeypatch, torn_down):
    _verified(monkeypatch, {"user_id": "1784100", "algorithm": "HMAC-SHA256"})
    resp = await client.post("/api/v1/webhooks/meta/instagram/deauthorize", data={"signed_request": "ok.payload"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert torn_down == [("instagram", "1784100")]


@pytest.mark.asyncio
async def test_data_deletion_returns_url_and_code(client, monkeypatch, torn_down):
    _verified(monkeypatch, {"user_id": "1784100", "algorithm": "HMAC-SHA256"})
    resp = await client.post("/api/v1/webhooks/meta/meta_ads/data-deletion", data={"signed_request": "ok.payload"})
    assert resp.status_code == 200
    body = resp.json()
    # Meta requires both fields; the URL must be absolute for it to be followable,
    # and must be the SAME page declared as the app's data-deletion instructions
    # URL — one canonical answer, not a second page that can drift from it.
    assert body["url"].startswith("http")
    assert body["url"].endswith("/support#data-deletion")
    assert body["confirmation_code"]
    assert torn_down == [("meta_ads", "1784100")]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["deauthorize", "data-deletion"])
async def test_verified_payload_without_user_id_tears_down_nothing(client, monkeypatch, torn_down, route):
    """A signed payload naming nobody must not be resolved by guessing."""
    _verified(monkeypatch, {"algorithm": "HMAC-SHA256"})
    resp = await client.post(f"/api/v1/webhooks/meta/instagram/{route}", data={"signed_request": "ok.payload"})
    assert resp.status_code == 200
    assert torn_down == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "stripe", "../../etc", "unknown"])
async def test_non_meta_provider_is_404(client, monkeypatch, torn_down, provider):
    """The path segment is attacker-controlled — only Meta-backed providers may
    reach the teardown."""
    _verified(monkeypatch, {"user_id": "1", "algorithm": "HMAC-SHA256"})
    resp = await client.post(f"/api/v1/webhooks/meta/{provider}/deauthorize", data={"signed_request": "ok.payload"})
    assert resp.status_code == 404
    assert torn_down == []


@pytest.mark.asyncio
async def test_unknown_meta_user_is_200_not_an_error(client, monkeypatch):
    """Nothing to revoke is a success: the user may have connected before we
    captured the id. Meta only requires that we answer."""
    _verified(monkeypatch, {"user_id": "nobody", "algorithm": "HMAC-SHA256"})
    monkeypatch.setattr(
        meta_account_callbacks.integrations_repo, "find_by_provider_user_id", AsyncMock(return_value=None)
    )
    resp = await client.post("/api/v1/webhooks/meta/instagram/deauthorize", data={"signed_request": "ok.payload"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_real_teardown_calls_the_canonical_disconnect(monkeypatch):
    """_teardown must reuse routers.connections._disconnect_teardown, not a
    second revocation path that could drift from the user-initiated one."""
    monkeypatch.setattr(
        meta_account_callbacks.integrations_repo,
        "find_by_provider_user_id",
        AsyncMock(return_value={"owner_id": "org_7", "provider": "instagram"}),
    )
    seen: list[tuple] = []

    async def _fake_disconnect(owner_id, provider, p):
        seen.append((owner_id, provider))

    import routers.connections as connections

    monkeypatch.setattr(connections, "_disconnect_teardown", _fake_disconnect)

    owner = await meta_account_callbacks._teardown("instagram", "1784100")

    assert owner == "org_7"
    assert seen == [("org_7", "instagram")]


@pytest.mark.asyncio
async def test_real_teardown_is_a_noop_for_an_unknown_id(monkeypatch):
    monkeypatch.setattr(
        meta_account_callbacks.integrations_repo, "find_by_provider_user_id", AsyncMock(return_value=None)
    )
    seen: list[tuple] = []

    async def _fake_disconnect(owner_id, provider, p):
        seen.append((owner_id, provider))

    import routers.connections as connections

    monkeypatch.setattr(connections, "_disconnect_teardown", _fake_disconnect)

    assert await meta_account_callbacks._teardown("instagram", "nobody") is None
    assert seen == []
