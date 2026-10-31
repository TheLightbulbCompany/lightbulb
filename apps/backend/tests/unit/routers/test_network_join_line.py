"""``/network/team``, ``/join-line`` and ``/device/approve-desktop``: a paired Mac (or a signed-in
computer) is handed its workspace's network. A Clerk token is refused, and no route logs a ticket."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import clerk_admin, container_rpc, network_login, network_member
from core.services.machine_auth import MachineIdentity
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401
from tests.unit.routers.test_network_login import Computer, _poll, _start

TICKET = "TICKET-secret-xyz"


def _device(user="user_ada", owner="org_a"):
    return MachineIdentity(
        user_id=user,
        org_id="" if owner == user else owner,
        owner_id=owner,
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="dev1",
    )


@pytest.fixture
def env(table, monkeypatch):
    from routers import network

    monkeypatch.setattr(network_member, "_humans_to_ask", AsyncMock(), raising=False)
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=["user_ada"]))
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ada"))
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=True))
    monkeypatch.setattr(network_member, "stored_team_id", AsyncMock(return_value="tm1"))
    mint = AsyncMock(return_value={"ticket": TICKET, "teamId": "tm1", "expiresAt": 99})
    monkeypatch.setattr(network_member, "mint_line", mint)
    rpc = AsyncMock()
    monkeypatch.setattr(container_rpc, "call", rpc)
    who = {"device": _device()}

    async def verify(token, client_id):
        return who["device"] if token == "device-token" else None  # a Clerk JWT is not one

    app = FastAPI()
    app.include_router(network.router, prefix="/api/v1/network")
    with (
        patch("core.services.machine_auth.verify_upgrade", verify),
        patch("core.dynamodb._table_prefix", "test-"),
        TestClient(app) as client,
    ):
        yield client, who, mint, rpc


AUTH = {"Authorization": "Bearer device-token"}


def test_join_line_refuses_a_clerk_token(env):
    client, _, mint, _ = env
    for path in ("/join-line", "/device/approve-desktop"):
        assert (
            client.post(
                f"/api/v1/network{path}", json={"userCode": "X"}, headers={"Authorization": "Bearer clerk.jwt"}
            ).status_code
            == 403
        )
    assert client.get("/api/v1/network/team", headers={"Authorization": "Bearer clerk.jwt"}).status_code == 403
    mint.assert_not_awaited()


def test_join_line_device_token_returns_ticket_no_store(env):
    client, _, mint, _ = env
    r = client.post("/api/v1/network/join-line", headers=AUTH)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"ticket": TICKET, "teamId": "tm1", "expiresAt": 99, "workspace": "Acme", "name": "Ada"}
    mint.assert_awaited_once_with("org_a")


def test_join_line_not_a_member_403(env, monkeypatch):
    client, _, mint, _ = env
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=["someone"]))
    assert client.post("/api/v1/network/join-line", headers=AUTH).status_code == 403
    mint.assert_not_awaited()


def test_join_line_free_409(env):
    client, who, mint, _ = env
    who["device"] = _device(owner="user_ada")
    r = client.post("/api/v1/network/join-line", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NO_WORKSPACE"
    mint.assert_not_awaited()


def test_join_line_no_sessions_409(env, monkeypatch):
    client, _, mint, _ = env
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=False))
    assert client.post("/api/v1/network/join-line", headers=AUTH).status_code == 409
    mint.assert_not_awaited()


def test_join_line_unavailable_is_503_with_retry_after(env):
    client, _, mint, _ = env
    mint.side_effect = container_rpc._unavailable()
    r = client.post("/api/v1/network/join-line", headers=AUTH)
    assert r.status_code == 503 and r.headers["retry-after"] == "10" and r.headers["cache-control"] == "no-store"


def test_join_line_logs_no_ticket(env, caplog):
    client, *_ = env
    caplog.set_level(logging.DEBUG)
    client.post("/api/v1/network/join-line", headers=AUTH)
    assert "TICKET" not in caplog.text


def test_team_makes_no_rpc(env):
    client, _, _, rpc = env
    r = client.get("/api/v1/network/team", headers=AUTH)
    assert r.json() == {"workspace": "Acme", "teamId": "tm1", "userId": "user_ada", "name": "Ada"}
    assert r.headers["cache-control"] == "no-store"
    rpc.assert_not_awaited()


def test_team_free_workspace_null(env):
    client, who, _, rpc = env
    who["device"] = _device(owner="user_ada")
    assert client.get("/api/v1/network/team", headers=AUTH).json() == {
        "workspace": None,
        "teamId": None,
        "userId": "user_ada",
        "name": None,
    }
    rpc.assert_not_awaited()


def test_approve_desktop_refuses_agents_purpose(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr("core.services.device_login.live_row", AsyncMock(return_value={"purpose": "agents"}))
    approve = AsyncMock()
    monkeypatch.setattr(network_login, "approve", approve)
    assert (
        client.post("/api/v1/network/device/approve-desktop", json={"userCode": "ABC"}, headers=AUTH).status_code == 403
    )
    approve.assert_not_awaited()


def test_approve_desktop_mints_the_login_proof(env, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "test-proof-seed")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")
    client, *_ = env
    computer = Computer()
    started = _start(client, computer).json()
    r = client.post("/api/v1/network/device/approve-desktop", json={"userCode": started["userCode"]}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    polled = _poll(client, started["code"])
    assert polled["proof"] and polled["name"] == "Ada"


def test_approve_desktop_needs_a_device_token(env):
    client, *_ = env
    assert client.post("/api/v1/network/device/approve-desktop", json={"userCode": "ABC"}).status_code == 403
