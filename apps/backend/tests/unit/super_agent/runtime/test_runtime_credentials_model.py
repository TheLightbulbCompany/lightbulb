"""Spec 13 §5.1-5.2: ``model`` + ``expires_at`` on the vend route (additive) and the forced-refresh
``/model`` route."""

from unittest.mock import AsyncMock

import pytest

from core.services import token_custody
from core.services.credentials.access_token import ReconnectRequired
from core.services.owner_lifecycle import OwnerLifecycleBusyError

URL = "/api/v1/runtime/credentials/u/org_a"
HDR = {"Authorization": "Bearer hook-a"}
VEND = token_custody.CodexVend("AT-model", "id", "acct", 1_900_000_000, False)


@pytest.fixture
def wired(monkeypatch):
    from core.services import container_auth
    from routers import runtime_rpc

    monkeypatch.setattr(
        container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-a"})
    )
    monkeypatch.setattr(runtime_rpc.messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "ceo"}]))
    monkeypatch.setattr(
        runtime_rpc.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "microsoft365", "status": "connected"}]),
    )
    monkeypatch.setattr(runtime_rpc.access_token, "get_with_expiry", AsyncMock(return_value=("graph", 1_800_000_000)))
    vend = AsyncMock(return_value=VEND)
    monkeypatch.setattr(runtime_rpc.token_custody, "codex_vend", vend)
    return {"rpc": runtime_rpc, "vend": vend}


@pytest.mark.asyncio
async def test_model_absent_for_actor(async_client, wired):
    wired["vend"].return_value = None  # not ChatGPT, or custody actor
    body = (await async_client.post(URL, json={"agent_id": "ceo"}, headers=HDR)).json()
    assert "model" not in body
    assert body["tokens"] == {"microsoft365": "graph"}


@pytest.mark.asyncio
async def test_expires_at_additive(async_client, wired):
    r = await async_client.post(URL, json={"agent_id": "ceo"}, headers=HDR)
    body = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert body["model"] == {
        "provider": "openai-codex",
        "access_token": "AT-model",
        "id_token": "id",
        "account_id": "acct",
        "expires_at": 1_900_000_000,
    }
    assert body["expires_at"] == {"microsoft365": 1_800_000_000, "model": 1_900_000_000}


@pytest.mark.asyncio
async def test_lease_busy_returns_409_and_vend_lists_unavailable(async_client, wired):
    wired["vend"].side_effect = OwnerLifecycleBusyError("held")
    body = (await async_client.post(URL, json={"agent_id": "ceo"}, headers=HDR)).json()
    assert body["unavailable"] == ["openai-codex"] and body["tokens"] == {"microsoft365": "graph"}
    r = await async_client.post(
        URL + "/model", json={"agent_id": "ceo", "presented_sha256": "a" * 64, "force": True}, headers=HDR
    )
    assert r.status_code == 409 and r.json()["detail"] == {"code": "busy"}


@pytest.mark.asyncio
async def test_model_route_codes(async_client, wired):
    payload = {"agent_id": "ceo", "presented_sha256": "b" * 64, "force": True}
    r = await async_client.post(URL + "/model", json=payload, headers=HDR)
    assert r.status_code == 200 and r.json()["model"]["access_token"] == "AT-model"
    assert r.json()["refreshed"] is False and r.headers["cache-control"] == "no-store"
    wired["vend"].assert_awaited_with("org_a", presented_sha256="b" * 64, force=True)

    wired["vend"].side_effect = ReconnectRequired("openai-codex")
    r = await async_client.post(URL + "/model", json=payload, headers=HDR)
    assert r.status_code == 410 and r.json()["detail"] == {"code": "reconnect_required"}

    wired["vend"].side_effect = None
    wired["vend"].return_value = None
    assert (await async_client.post(URL + "/model", json=payload, headers=HDR)).status_code == 404

    # Same hook-token and roster checks as the vend route.
    assert (
        await async_client.post(URL + "/model", json=payload, headers={"Authorization": "Bearer x"})
    ).status_code == 401
    other = {**payload, "agent_id": "stranger"}
    assert (await async_client.post(URL + "/model", json=other, headers=HDR)).status_code == 403


@pytest.mark.asyncio
async def test_force_requires_presented_hash(async_client, wired):
    r = await async_client.post(URL + "/model", json={"agent_id": "ceo", "force": True}, headers=HDR)
    assert r.status_code == 400  # the app answers request validation errors with 400
    wired["vend"].assert_not_awaited()
