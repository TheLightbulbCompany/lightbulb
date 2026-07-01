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
    tok = platform_ops_token.mint("org_founder")
    with (
        patch.object(ops_router.ecs_ops, "fleet_counts_live", AsyncMock(return_value={"total": 1})),
        patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=[{"owner_id": "org_a"}])),
    ):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["total"] == 1


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
    with (
        patch.object(ops_router.ecs_ops, "fleet_counts_live", AsyncMock(side_effect=RuntimeError("throttled"))),
        patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=[])),
    ):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] != "ok"  # NOT rendered as all-clear
    assert body["errors"] and body["errors"][0]["domain"] == "ecs"
