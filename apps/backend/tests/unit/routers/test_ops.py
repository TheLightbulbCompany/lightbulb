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
        user_id="user_admin", email="admin@isol8.co", human_session=True
    )


def _as_non_admin(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_customer", email="customer@example.com", human_session=True
    )


@pytest.mark.parametrize("path", ["spark"])
def test_ops_reads_reject_missing_credentials(client, path):
    assert client.get(f"/api/v1/ops/{path}").status_code == 401


def test_ops_reads_reject_invalid_credentials(client):
    with patch(
        "core.auth._decode_token",
        AsyncMock(side_effect=jwt.InvalidTokenError("not a Clerk JWT")),
    ):
        response = client.get("/api/v1/ops/spark", headers=_auth("retired-agent-token"))

    assert response.status_code == 401


def test_ops_reads_reject_clerk_non_admin(app, client):
    _as_non_admin(app)
    response = client.get("/api/v1/ops/spark", headers=_auth("clerk-token"))
    assert response.status_code == 403
    assert response.json()["detail"] == "Platform admin access required"


def test_token_mint_route_is_removed(app, client):
    _as_admin(app)
    response = client.post("/api/v1/ops/token", headers=_auth("clerk-token"))
    assert response.status_code == 404
