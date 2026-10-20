"""Device login: `lightbulb join` starts, the browser approves (pairs + enrolls as the
signed-in person, server-side), and the CLI polls the result exactly once."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import clerk_admin, device_login, local_sessions
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

ORG_A = "org_a"
ORG_B = "org_b"
MAC = "a" * 64
CLI = "b" * 64


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


KEYS = {"tauriMacPubKey": _pem(), "cliPubKey": _pem(), "machineName": "Ada's MacBook"}


@pytest.fixture
def env(table, monkeypatch):
    from routers import desktop_auth, desktop_pair

    monkeypatch.setattr(clerk_admin, "get_user", AsyncMock(return_value=None))
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value=None))
    from core.services import messaging

    messaging._display_names.clear()  # a name cached by another test would leak into this one
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")
    pair = AsyncMock(
        return_value=MagicMock(
            gateway_url="wss://ws-dev.example",
            gateway_token="tok",
            device_id_mac=MAC,
            device_id_cli=CLI,
            commands_mac=("system.notify",),
            commands_cli=("system.run",),
        )
    )
    monkeypatch.setattr(desktop_pair, "pair_desktop_devices", pair)

    async def enroll(ctx, request):
        return local_sessions.EnrollmentResponse(
            device_id=request.device_id,
            cli_device_id=request.cli_device_id,
            human_user_id=ctx.user_id,
            owner_id=ctx.org_id,
            enabled=True,
            revision="rev1",
        )

    enroll_mock = AsyncMock(side_effect=enroll)
    monkeypatch.setattr(local_sessions, "set_enrollment", enroll_mock)

    app = FastAPI()
    app.include_router(desktop_auth.router, prefix="/api/v1/auth")
    who = {"ctx": AuthContext(user_id="user_alice", org_id=ORG_A)}

    async def _override():
        return who["ctx"]

    app.dependency_overrides[get_current_user] = _override
    with TestClient(app) as client:
        yield client, who, pair, enroll_mock


def _start(client):
    r = client.post("/api/v1/auth/device/start", json=KEYS)
    assert r.status_code == 200, r.text
    return r.json()


def _poll(client, code):
    r = client.post("/api/v1/auth/device/poll", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def test_start_then_poll_is_pending(env):
    client, *_ = env
    started = _start(client)
    assert started["interval"] == 2 and started["expiresIn"] == 600
    assert len(started["code"]) >= 43
    user_code = started["userCode"]
    assert len(user_code) == 9 and user_code[4] == "-"
    assert not set(user_code.replace("-", "")) & set("0O1I")
    assert started["verifyUrl"] == f"https://dev.example/auth/connect-machine?code={user_code}"
    assert _poll(client, started["code"]) == {"status": "pending"}
    assert _poll(client, "not-a-code") == {"status": "expired"}


def test_approve_pairs_and_enrolls_then_poll_returns_once(env):
    client, who, pair, enroll = env
    started = _start(client)
    r = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert r.status_code == 200, r.text

    kwargs = pair.await_args.kwargs
    assert kwargs["user_id"] == "user_alice" and kwargs["owner_id"] == ORG_A
    assert kwargs["tauri_mac_pubkey"] == KEYS["tauriMacPubKey"] and kwargs["cli_pubkey"] == KEYS["cliPubKey"]
    perms = kwargs["permissions"]
    assert (perms.screen, perms.shell, perms.browser, perms.files) == (False, False, False, False)
    request = enroll.await_args.args[1]
    assert (request.device_id, request.cli_device_id, request.enabled) == (MAC, CLI, True)

    # Idempotent for the same approver.
    again = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert again.status_code == 200, again.text

    first = _poll(client, started["code"])
    second = _poll(client, started["code"])
    assert first["status"] == "approved" and first["pair"]["ownerId"] == ORG_A
    assert first["pair"]["deviceIdMac"] == MAC
    assert first["pair"]["gatewayToken"] == ""  # the unauthenticated poll never gets the gateway token
    assert "tok" not in str(first)
    assert first["enrollment"]["deviceId"] == MAC and first["enrollment"]["ownerId"] == ORG_A
    assert first["humanUserId"] == "user_alice"
    assert set(first) == {"status", "pair", "enrollment", "humanUserId", "approverName", "workspaceName"}
    # Unknown names (the lookup answered the raw id, no org) are None, never the id.
    assert first["approverName"] is None and first["workspaceName"] is None
    assert second["status"] == "expired"  # single use
    gone = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert gone.status_code == 410


def test_approve_by_someone_else_is_refused(env):
    client, who, pair, _ = env
    started = _start(client)
    assert client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]}).status_code == 200
    who["ctx"] = AuthContext(user_id="user_mallory", org_id=ORG_A)
    r = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert r.status_code == 409
    assert pair.await_count == 1


def test_code_expires_after_600s(env, monkeypatch):
    client, *_ = env
    started = _start(client)
    real = device_login.time.time
    monkeypatch.setattr(device_login.time, "time", lambda: real() + 601)
    assert _poll(client, started["code"]) == {"status": "expired"}
    r = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert r.status_code == 410


def test_approve_uses_the_approvers_own_workspace(env):
    client, who, pair, _ = env
    started = _start(client)
    who["ctx"] = AuthContext(user_id="user_bob", org_id=ORG_B)
    assert client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]}).status_code == 200
    assert pair.await_args.kwargs["owner_id"] == ORG_B
    assert pair.await_args.kwargs["user_id"] == "user_bob"
    first = _poll(client, started["code"])
    assert first["pair"]["ownerId"] == ORG_B and first["humanUserId"] == "user_bob"


def test_approve_without_org_is_409_workspace_required(env):
    client, who, pair, _ = env
    started = _start(client)
    who["ctx"] = AuthContext(user_id="user_solo", org_id=None)
    r = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "workspace_required"
    pair.assert_not_awaited()
    assert _poll(client, started["code"]) == {"status": "pending"}


def test_start_rejects_a_bad_pem(env):
    client, *_ = env
    r = client.post("/api/v1/auth/device/start", json={**KEYS, "cliPubKey": "-----BEGIN PUBLIC KEY-----\nnope\n"})
    assert r.status_code == 422


def test_machine_name_is_cleaned_and_bounded(env):
    client, *_ = env
    started = client.post("/api/v1/auth/device/start", json={**KEYS, "machineName": " Ada\x1b[31m\n Mac\u200b "}).json()
    r = client.post("/api/v1/auth/device/describe", json={"userCode": started["userCode"]})
    assert r.status_code == 200 and r.json() == {"machineName": "Ada[31m Mac"}
    too_long = client.post("/api/v1/auth/device/start", json={**KEYS, "machineName": "x" * 65})
    assert too_long.status_code == 422


def test_a_row_taken_between_read_and_claim_is_expired_not_conflict(env, monkeypatch):
    from core.repositories import messaging_repo

    client, *_ = env
    started = _start(client)
    real_claim = messaging_repo.claim_device_login

    async def racing_claim(code_hash, owner_id, user_id):
        await messaging_repo.take_device_login(code_hash, approved_only=False)
        return await real_claim(code_hash, owner_id, user_id)

    monkeypatch.setattr(messaging_repo, "claim_device_login", racing_claim)
    r = client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]})
    assert r.status_code == 410


def test_poll_names_the_approver_and_the_workspace(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(clerk_admin, "get_user", AsyncMock(return_value={"first_name": "Alice", "last_name": "Ng"}))
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme Builders"}))
    started = _start(client)
    assert client.post("/api/v1/auth/device/approve", json={"userCode": started["userCode"]}).status_code == 200
    first = _poll(client, started["code"])
    assert (first["approverName"], first["workspaceName"]) == ("Alice Ng", "Acme Builders")
