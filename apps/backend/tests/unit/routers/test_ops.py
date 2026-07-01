import os

os.environ.setdefault("PLATFORM_OPS_TOKEN_KEY", "test-ops-key-abc")
os.environ.setdefault("PAPERCLIP_SERVICE_TOKEN_KEY", "test-paperclip-key-xyz")

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.services import platform_ops_token, service_token
import routers.ops as ops_router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_OPS_OWNER_ID", "org_founder")
    app = FastAPI()
    app.include_router(ops_router.router, prefix="/api/v1/ops")
    return TestClient(app, raise_server_exceptions=False)


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_fleet_accepts_platform_ops_token(client):
    # The router reads fleet_grid() ONCE and derives counts locally (no second
    # fleet_counts_live() read). A valid empty grid must stay source_state=ok.
    tok = platform_ops_token.mint("org_founder")
    grid = [{"owner_id": "org_a", "service_name": "s", "status": "running"}]
    with patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=grid)):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["running"] == 1
    assert body["data"]["counts"]["total"] == 1
    assert body["data"]["grid"] == grid


def test_fleet_empty_grid_is_ok_not_unavailable(client):
    # Regression: a successful but EMPTY grid ([] after a reset / zero services)
    # must report source_state=ok, never unavailable (the truthiness-bug fix).
    tok = platform_ops_token.mint("org_founder")
    with patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=[])):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["total"] == 0
    assert body["errors"] == []


def test_fleet_rejects_tenant_paperclip_token(client):
    tenant = service_token.mint("org_founder")  # ubiquitous tenant token
    res = client.get("/api/v1/ops/fleet", headers=_auth(tenant))
    assert res.status_code == 401


def test_fleet_rejects_platform_ops_token_for_wrong_owner(client):
    tok = platform_ops_token.mint("org_not_founder")
    res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 401


def test_fleet_degrades_when_source_unavailable(client):
    tok = platform_ops_token.mint("org_founder")
    with patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(side_effect=RuntimeError("throttled"))):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] != "ok"  # NOT rendered as all-clear
    assert body["errors"] and body["errors"][0]["domain"] == "ecs"
    assert body["data"]["grid"] is None
    assert body["data"]["counts"] is None


def test_alarms_accepts_platform_ops_token(client):
    tok = platform_ops_token.mint("org_founder")
    with patch.object(
        ops_router.alarms,
        "alarms_in_alarm",
        AsyncMock(return_value=[{"name": "isol8-dev-5xx", "metric": "HTTPCode_Target_5XX_Count"}]),
    ):
        res = client.get("/api/v1/ops/alarms", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] == "ok"
    assert body["data"][0]["name"] == "isol8-dev-5xx"


def test_alarms_rejects_tenant_paperclip_token(client):
    tenant = service_token.mint("org_founder")
    res = client.get("/api/v1/ops/alarms", headers=_auth(tenant))
    assert res.status_code == 401


def test_alarms_degrades_when_source_unavailable(client):
    tok = platform_ops_token.mint("org_founder")
    with patch.object(
        ops_router.alarms,
        "alarms_in_alarm",
        AsyncMock(side_effect=RuntimeError("throttled")),
    ):
        res = client.get("/api/v1/ops/alarms", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] != "ok"  # degrade, never all-clear
    assert body["errors"] and body["errors"][0]["domain"] == "cloudwatch"
    assert body["data"] == []
