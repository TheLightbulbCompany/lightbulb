from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
import routers.ops as ops_router


@pytest.fixture
def app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(ops_router.router, prefix="/api/v1/ops")
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _as_admin(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin",
        email="admin@isol8.co",
    )


def _as_non_admin(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_customer",
        email="customer@example.com",
    )


def test_fleet_accepts_clerk_platform_admin(app, client):
    _as_admin(app)
    grid = [{"owner_id": "org_a", "service_name": "s", "status": "running"}]
    with patch.object(ops_router.fleet, "fleet_grid", AsyncMock(return_value=grid)):
        response = client.get("/api/v1/ops/fleet", headers=_auth("clerk-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["running"] == 1
    assert body["data"]["counts"]["total"] == 1
    assert body["data"]["grid"] == grid


def test_fleet_empty_grid_is_ok_not_unavailable(app, client):
    _as_admin(app)
    with patch.object(ops_router.fleet, "fleet_grid", AsyncMock(return_value=[])):
        response = client.get("/api/v1/ops/fleet", headers=_auth("clerk-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["total"] == 0
    assert body["errors"] == []


def test_fleet_degrades_when_source_unavailable(app, client):
    _as_admin(app)
    with patch.object(
        ops_router.fleet,
        "fleet_grid",
        AsyncMock(side_effect=RuntimeError("throttled")),
    ):
        response = client.get("/api/v1/ops/fleet", headers=_auth("clerk-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["source_state"] != "ok"
    assert body["errors"] and body["errors"][0]["domain"] == "ecs"
    assert body["data"]["grid"] is None
    assert body["data"]["counts"] is None


def test_deploys_accepts_clerk_platform_admin(app, client):
    _as_admin(app)
    deploys = [{"stack": "isol8-dev-service", "status": "UPDATE_COMPLETE"}]
    with patch.object(ops_router.deploys, "list_stacks", AsyncMock(return_value=deploys)):
        response = client.get("/api/v1/ops/deploys", headers=_auth("clerk-token"))

    assert response.status_code == 200
    assert response.json()["data"] == deploys


def test_alarms_accepts_clerk_platform_admin(app, client):
    _as_admin(app)
    alarms = [{"name": "isol8-dev-5xx", "metric": "HTTPCode_Target_5XX_Count"}]
    with patch.object(ops_router.alarms, "alarms_in_alarm", AsyncMock(return_value=alarms)):
        response = client.get("/api/v1/ops/alarms", headers=_auth("clerk-token"))

    assert response.status_code == 200
    assert response.json()["data"] == alarms


def test_alarms_degrades_when_source_unavailable(app, client):
    _as_admin(app)
    with patch.object(
        ops_router.alarms,
        "alarms_in_alarm",
        AsyncMock(side_effect=RuntimeError("throttled")),
    ):
        response = client.get("/api/v1/ops/alarms", headers=_auth("clerk-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["source_state"] != "ok"
    assert body["errors"] and body["errors"][0]["domain"] == "cloudwatch"
    assert body["data"] == []


@pytest.mark.parametrize("path", ["fleet", "deploys", "alarms"])
def test_ops_reads_reject_missing_credentials(client, path):
    assert client.get(f"/api/v1/ops/{path}").status_code == 401


def test_ops_reads_reject_invalid_credentials(client):
    with patch(
        "core.auth._decode_token",
        AsyncMock(side_effect=jwt.InvalidTokenError("not a Clerk JWT")),
    ):
        response = client.get("/api/v1/ops/fleet", headers=_auth("retired-agent-token"))

    assert response.status_code == 401


def test_ops_reads_reject_clerk_non_admin(app, client):
    _as_non_admin(app)
    response = client.get("/api/v1/ops/fleet", headers=_auth("clerk-token"))
    assert response.status_code == 403
    assert response.json()["detail"] == "Platform admin access required"


def test_token_mint_route_is_removed(app, client):
    _as_admin(app)
    response = client.post("/api/v1/ops/token", headers=_auth("clerk-token"))
    assert response.status_code == 404
