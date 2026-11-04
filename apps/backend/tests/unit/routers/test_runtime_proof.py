"""The backend mints a workspace proof for its own runtime's member key (A2A phase 2b)."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import network_proof
from core.services.network_proof import _unb64
from routers import a2a

ORG = "org_1"
TOKEN = "hook-token"
KEY = "k" * 52
URL = f"/api/v1/internal/network/{ORG}/runtime-proof"
H = {"Authorization": f"Bearer {TOKEN}"}


def _parts(jws):
    h, p, _ = jws.split(".")
    return json.loads(_unb64(h)), json.loads(_unb64(p))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    monkeypatch.setattr(
        "core.services.container_auth.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": ORG, "hook_token": TOKEN}),
    )
    monkeypatch.setattr("core.services.agentcore_runtime.network_member_key", AsyncMock(return_value=KEY))
    monkeypatch.setattr("core.services.network_member.stored_team_id", AsyncMock(return_value="team-1"))
    app = FastAPI()
    app.include_router(a2a.router)
    return TestClient(app)


def test_mint_workspace_claims(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    header, claims = _parts(network_proof.mint_workspace(KEY, "org_1", now=1000))
    assert claims["sub"] == claims["org"] == "org_1"
    assert claims["computer"] == KEY
    assert claims["exp"] - claims["iat"] == 30 * 86_400
    assert set(claims) == {"iat", "exp", "sub", "org", "computer"}
    assert header["kid"] == network_proof.kid()


def test_route_mints_for_the_runtimes_own_key(client):
    r = client.post(URL, json={"computer": "someone-elses-key"}, headers=H)
    assert r.status_code == 200
    _, claims = _parts(r.json()["proof"])
    assert claims["computer"] == KEY
    assert claims["sub"] == ORG
    assert r.json()["exp"] == claims["exp"]


def test_route_without_a_runtime_is_404(client, monkeypatch):
    monkeypatch.setattr("core.services.agentcore_runtime.network_member_key", AsyncMock(return_value=None))
    r = client.post(URL, headers=H)
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "no_runtime"}


def test_route_needs_the_hook_token(client):
    assert client.post(URL).status_code == 401
    assert client.post(URL, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_route_refuses_a_workspace_off_its_network(client, monkeypatch):
    monkeypatch.setattr("core.services.network_member.stored_team_id", AsyncMock(return_value=None))
    r = client.post(URL, headers=H)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "not_on_network"}
