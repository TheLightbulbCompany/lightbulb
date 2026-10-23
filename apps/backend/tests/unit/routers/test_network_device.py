"""Lightbulb Connect sign-in: a computer starts a typed device code, a signed-in person
approves it, the computer polls its proof once, and later refreshes it with its own key."""

# ruff: noqa: F811 -- `table`, `issuer` and `audit` are fixtures imported and requested by name.

import base64
import hashlib
import logging
import time
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import clerk_admin, messaging, network_issuer
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401
from tests.unit.services.test_network_issuer import audit, issuer, member_key, open_proof  # noqa: F401

ORG_A = "org_a"
COMPUTER = Ed25519PrivateKey.generate()
KEY = member_key(COMPUTER)
HOST = "Ada's MacBook"


@pytest.fixture
def env(issuer, audit, monkeypatch):
    from routers import desktop_auth, network

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Ada Lovelace"))
    clerk = {
        "orgs": [{"id": ORG_A, "name": "Acme", "slug": "acme", "role": "org:member"}],
        "user": {"id": "user_ada", "first_name": "Ada", "last_name": "Lovelace"},
    }
    monkeypatch.setattr(clerk_admin, "list_user_organizations", AsyncMock(side_effect=lambda *a, **k: clerk["orgs"]))
    monkeypatch.setattr(clerk_admin, "get_user_strict", AsyncMock(side_effect=lambda *a, **k: clerk["user"]))
    clerk["org"] = {"id": ORG_A, "name": "Acme"}
    monkeypatch.setattr(clerk_admin, "get_organization_strict", AsyncMock(side_effect=lambda *a, **k: clerk["org"]))

    app = FastAPI()
    app.include_router(network.router, prefix="/api/v1/network")
    app.include_router(desktop_auth.router, prefix="/api/v1/auth")
    who = {"ctx": AuthContext(user_id="user_ada", org_id=ORG_A)}

    async def _override():
        return who["ctx"]

    app.dependency_overrides[get_current_user] = _override
    with TestClient(app) as client:
        yield client, who, clerk


def _start(client, key=KEY):
    r = client.post("/api/v1/network/device/start", json={"member_key": key, "computer": HOST})
    assert r.status_code == 200, r.text
    return r.json()


def _token(client, device_code):
    return client.post("/api/v1/network/device/token", json={"device_code": device_code})


def _approve(client, user_code):
    r = client.post("/api/v1/network/device/approve", json={"user_code": user_code})
    assert r.status_code == 200, r.text


def _proof(client):
    started = _start(client)
    _approve(client, started["user_code"])
    return _token(client, started["device_code"]).json()["proof"]


def _signed(proof: str, at: int, key=COMPUTER) -> dict:
    sig = key.sign(network_issuer.canonical({"proof": proof, "at": at}))
    return {"proof": proof, "at": at, "sig": base64.urlsafe_b64encode(sig).decode().rstrip("=")}


def test_start_uri_carries_no_code(env):
    client, *_ = env
    started = _start(client)
    assert started["verification_uri_complete"] == "https://dev.example/device"
    assert started["interval"] == 2
    assert started["user_code"] not in started["verification_uri_complete"]
    assert started["device_code"] not in started["verification_uri_complete"]
    bad = client.post("/api/v1/network/device/start", json={"member_key": KEY.upper(), "computer": HOST})
    assert bad.status_code == 422


def test_describe_shows_name_and_key_short(env):
    client, *_ = env
    started = _start(client)
    r = client.post("/api/v1/network/device/describe", json={"user_code": started["user_code"].lower()})
    assert r.status_code == 200, r.text
    assert r.json() == {"computer_name": HOST, "key_short": KEY[:12]}


def test_token_is_pending_then_proof_once_then_410(env):
    client, *_ = env
    started = _start(client)
    first = _token(client, started["device_code"])
    assert (first.status_code, first.json()) == (200, {"pending": True})
    _approve(client, started["user_code"])
    _approve(client, started["user_code"])  # a retried approve is harmless
    got = _token(client, started["device_code"])
    assert got.status_code == 200 and set(got.json()) == {"proof"}
    assert _token(client, started["device_code"]).status_code == 410
    assert _token(client, "never-issued").status_code == 410


def test_proof_names_the_key_not_the_hostname(env):
    client, *_ = env
    _, claims = open_proof(_proof(client))
    assert claims["computer"] == KEY
    assert HOST not in str(claims) and "MacBook" not in str(claims)
    assert claims["sub"] == "user_ada" and claims["name"] == "Ada Lovelace"


def test_approve_without_a_workspace_mints_a_proof(env):
    client, who, clerk = env
    who["ctx"] = AuthContext(user_id="user_ada")  # a free person: no org claim at all
    clerk["orgs"] = []
    _, claims = open_proof(_proof(client))
    assert claims["kind"] == "computer" and "workspace" not in claims


def test_computer_proof_carries_the_active_workspace(env):
    client, *_ = env
    _, claims = open_proof(_proof(client))
    assert claims["workspace"] == {"id": ORG_A, "name": "Acme"}
    assert "agents" not in claims


def test_device_code_stored_only_as_sha256(env, table):
    client, *_ = env
    started = _start(client)
    _approve(client, started["user_code"])
    rows = table.scan()["Items"]
    assert started["device_code"] not in str(rows)
    assert hashlib.sha256(started["device_code"].encode()).hexdigest() in str(rows)
    login = next(r for r in rows if r["pk"].startswith("DEVICELOGIN#"))
    assert (login["purpose"], login["member_key"], login["computer_name"]) == ("network", KEY, HOST)


def test_network_code_is_invisible_to_the_desktop_flow(env):
    client, *_ = env
    started = _start(client)
    r = client.post("/api/v1/auth/device/describe", json={"userCode": started["user_code"]})
    assert r.status_code == 410
    polled = client.post("/api/v1/auth/device/poll", json={"code": started["device_code"]})
    assert polled.json() == {"status": "expired"}
    assert _token(client, started["device_code"]).json() == {"pending": True}  # left in place


def test_refresh_requires_the_computer_signature(env):
    client, *_ = env
    proof = _proof(client)
    now = int(time.time())
    stranger = Ed25519PrivateKey.generate()
    assert client.post("/api/v1/network/proof/refresh", json=_signed(proof, now, stranger)).status_code == 401
    assert client.post("/api/v1/network/proof/refresh", json=_signed(proof, now - 301)).status_code == 401
    tampered = _signed(proof, now)
    tampered["at"] = now + 1
    assert client.post("/api/v1/network/proof/refresh", json=tampered).status_code == 401
    r = client.post("/api/v1/network/proof/refresh", json=_signed(proof, now))
    assert r.status_code == 200, r.text
    _, claims = open_proof(r.json()["proof"])
    assert claims["computer"] == KEY and claims["iat"] >= now and claims["workspace"]["id"] == ORG_A


@pytest.mark.asyncio
async def test_refresh_refuses_after_7_days_of_grace(env):
    client, _, clerk = env
    now = int(time.time())
    day = 86_400
    within = await network_issuer.mint("user_ada", "Ada", KEY, None, None, now - 7 * day)
    past = await network_issuer.mint("user_ada", "Ada", KEY, None, None, now - 8 * day - 1)
    assert client.post("/api/v1/network/proof/refresh", json=_signed(within, now)).status_code == 200
    assert client.post("/api/v1/network/proof/refresh", json=_signed(past, now)).status_code == 401
    clerk["user"] = None  # the Clerk user is gone
    assert client.post("/api/v1/network/proof/refresh", json=_signed(within, now)).status_code == 401
    for flag in ("banned", "locked"):
        clerk["user"] = {"id": "user_ada", flag: True}
        assert client.post("/api/v1/network/proof/refresh", json=_signed(within, now)).status_code == 401


@pytest.mark.asyncio
async def test_refresh_accepts_a_proof_from_the_previous_kid(env, monkeypatch):
    client, *_ = env
    old_seed, new_seed = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        network_issuer,
        "ISSUER_PUBLIC_KEYS",
        {"p1": old_seed.public_key().public_bytes_raw(), "p2": new_seed.public_key().public_bytes_raw()},
    )
    now = int(time.time())
    monkeypatch.setattr(settings, "NETWORK_ISSUER_KID", "p1")
    monkeypatch.setattr(network_issuer, "_key", (time.monotonic(), old_seed))
    old = await network_issuer.mint("user_ada", "Ada", KEY, None, None, now - 3600)
    monkeypatch.setattr(settings, "NETWORK_ISSUER_KID", "p2")  # rotated: p2 is now active
    monkeypatch.setattr(network_issuer, "_key", (time.monotonic(), new_seed))
    r = client.post("/api/v1/network/proof/refresh", json=_signed(old, now))
    assert r.status_code == 200, r.text
    header, _ = open_proof(r.json()["proof"], new_seed.public_key())
    assert header["kid"] == "p2"
    monkeypatch.setattr(network_issuer, "ISSUER_PUBLIC_KEYS", {"p2": new_seed.public_key().public_bytes_raw()})
    assert client.post("/api/v1/network/proof/refresh", json=_signed(old, now)).status_code == 401  # unknown kid


@pytest.mark.asyncio
async def test_refresh_of_a_workspace_proof_keeps_its_agents_and_rereads_the_name(env):
    client, _, clerk = env
    now = int(time.time())
    ws = {"id": ORG_A, "name": "Acme"}
    proof = await network_issuer.mint(f"ws:{ORG_A}", "Acme", KEY, ws, ["Friday", "Atlas"], now)
    clerk["org"] = {"id": ORG_A, "name": "Acme Renamed"}
    r = client.post("/api/v1/network/proof/refresh", json=_signed(proof, now))
    assert r.status_code == 200, r.text
    _, claims = open_proof(r.json()["proof"])
    assert (claims["sub"], claims["kind"], claims["name"]) == (f"ws:{ORG_A}", "workspace", "Acme Renamed")
    assert claims["workspace"] == {"id": ORG_A, "name": "Acme Renamed"} and claims["agents"] == ["Friday", "Atlas"]
    clerk["org"] = None  # the workspace is gone (a strict 404)
    assert client.post("/api/v1/network/proof/refresh", json=_signed(proof, now)).status_code == 401


@pytest.mark.asyncio
async def test_refresh_answers_503_when_clerk_is_unreachable(env, monkeypatch):
    client, *_ = env
    now = int(time.time())
    person = await network_issuer.mint("user_ada", "Ada", KEY, None, None, now)
    ws = {"id": ORG_A, "name": "Acme"}
    workspace = await network_issuer.mint(f"ws:{ORG_A}", "Acme", KEY, ws, ["Friday"], now)
    blip = AsyncMock(side_effect=clerk_admin.ClerkLookupError("network"))
    monkeypatch.setattr(clerk_admin, "get_user_strict", blip)
    monkeypatch.setattr(clerk_admin, "get_organization_strict", blip)
    for proof in (person, workspace):
        assert client.post("/api/v1/network/proof/refresh", json=_signed(proof, now)).status_code == 503


def test_refresh_drops_workspace_after_leaving(env):
    client, _, clerk = env
    proof = _proof(client)
    clerk["orgs"] = [{"id": ORG_A, "name": "Acme Renamed", "slug": "acme", "role": "org:member"}]
    r = client.post("/api/v1/network/proof/refresh", json=_signed(proof, int(time.time())))
    assert open_proof(r.json()["proof"])[1]["workspace"] == {"id": ORG_A, "name": "Acme Renamed"}
    clerk["orgs"] = []
    r = client.post("/api/v1/network/proof/refresh", json=_signed(proof, int(time.time())))
    assert r.status_code == 200 and "workspace" not in open_proof(r.json()["proof"])[1]


def test_routes_log_no_proof(env, caplog):
    client, *_ = env
    caplog.set_level(logging.DEBUG)
    proof = _proof(client)
    client.post("/api/v1/network/proof/refresh", json=_signed(proof, int(time.time())))
    client.post("/api/v1/network/proof/refresh", json=_signed(proof, 0))
    # botocore's own DEBUG wire dump (never on in a deployed task) is the AWS SDK's, not ours.
    ours = [r.getMessage() for r in caplog.records if not r.name.startswith(("botocore", "boto3"))]
    assert ours and "eyJ" not in "\n".join(ours)


def test_a_rejected_network_body_is_not_echoed():
    from main import app

    proof = "eyJ" + "x" * 5000  # too long: refused, and never echoed back
    r = TestClient(app).post("/api/v1/network/proof/refresh", json={"proof": proof, "at": 0, "sig": "s"})
    assert r.status_code == 400 and "eyJ" not in r.text
