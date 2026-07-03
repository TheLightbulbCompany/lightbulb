"""platform_ops token access to the read-only /admin/* debug surface.

The founder-agent's platform_ops token (verified by
core/services/admin_access.py) is accepted on curated GET endpoints and
nowhere else — every mutation stays Clerk-only. Mirrors test_ops.py: real
minted tokens against a bespoke app, service layer mocked.

Fall-through-to-Clerk cases patch ``core.auth._decode_token`` so
get_current_user fails deterministically without a JWKS network fetch.
"""

import os

os.environ.setdefault("PLATFORM_OPS_TOKEN_KEY", "test-ops-key-abc")

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.admin as admin_router
from core.auth import AuthContext
from core.config import settings
from core.services import platform_ops_token


def _clerk_rejects():
    return patch(
        "core.auth._decode_token",
        AsyncMock(side_effect=jwt.InvalidTokenError("not a clerk jwt")),
    )


def _make_client(monkeypatch, owner_id: str) -> TestClient:
    monkeypatch.setattr(settings, "PLATFORM_OPS_OWNER_ID", owner_id)
    app = FastAPI()
    app.include_router(admin_router.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(monkeypatch):
    return _make_client(monkeypatch, "org_founder")


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# Token accepted on the read surface
# ---------------------------------------------------------------------------


def test_system_health_accepts_platform_ops_token(client):
    tok = platform_ops_token.mint("org_founder")
    payload = {"fleet": {"total": 3, "running": 3}, "recent_errors": []}
    with patch("routers.admin.system_health.get_system_health", AsyncMock(return_value=payload)):
        res = client.get("/api/v1/admin/system/health", headers=_auth(tok))
    assert res.status_code == 200
    assert res.json() == payload


def test_user_overview_accepts_platform_ops_token(client):
    tok = platform_ops_token.mint("org_founder")
    payload = {"identity": {"id": "user_abc"}, "container": None, "billing": None, "usage": None}
    with patch("routers.admin.admin_service.get_overview", AsyncMock(return_value=payload)) as mock_overview:
        res = client.get("/api/v1/admin/users/user_abc/overview", headers=_auth(tok))
    assert res.status_code == 200
    assert res.json() == payload
    args = mock_overview.await_args.args
    assert (args[0] if args else mock_overview.await_args.kwargs.get("user_id")) == "user_abc"


def test_platform_ops_read_emits_trail_log(client, caplog):
    tok = platform_ops_token.mint("org_founder")
    jti = jwt.decode(tok, options={"verify_signature": False})["jti"]
    caplog.set_level(logging.INFO, logger="core.services.admin_access")
    with patch("routers.admin.system_health.get_system_health", AsyncMock(return_value={})):
        res = client.get("/api/v1/admin/system/health", headers=_auth(tok))
    assert res.status_code == 200
    trail = [r.getMessage() for r in caplog.records if "platform_ops admin read" in r.getMessage()]
    assert len(trail) == 1
    assert "/api/v1/admin/system/health" in trail[0]
    assert "sub=org_founder" in trail[0]
    assert f"jti={jti}" in trail[0]


# ---------------------------------------------------------------------------
# Token rejected everywhere else / on every failure mode
# ---------------------------------------------------------------------------


def test_mutation_rejects_platform_ops_token(client):
    """A valid ops token must never reach a mutation handler — the write path
    resolves auth via require_platform_admin (Clerk only)."""
    tok = platform_ops_token.mint("org_founder")
    ecs = MagicMock()
    with _clerk_rejects(), patch("routers.admin.get_ecs_manager", MagicMock(return_value=ecs)):
        res = client.post("/api/v1/admin/users/user_abc/container/reprovision", headers=_auth(tok))
    assert res.status_code == 401
    ecs.reprovision_for_user.assert_not_called()


def test_read_rejects_missing_credentials(client):
    res = client.get("/api/v1/admin/system/health")
    assert res.status_code == 401


def test_read_rejects_garbage_token(client):
    with _clerk_rejects():
        res = client.get("/api/v1/admin/system/health", headers=_auth("not-a-jwt"))
    assert res.status_code == 401


def test_read_rejects_expired_token(client):
    tok = platform_ops_token.mint("org_founder", ttl_days=-1)
    with _clerk_rejects():
        res = client.get("/api/v1/admin/system/health", headers=_auth(tok))
    assert res.status_code == 401


def test_read_rejects_token_for_wrong_owner(client):
    tok = platform_ops_token.mint("org_not_founder")
    with _clerk_rejects():
        res = client.get("/api/v1/admin/system/health", headers=_auth(tok))
    assert res.status_code == 401


def test_read_fails_closed_when_owner_id_unset(monkeypatch):
    """No PLATFORM_OPS_OWNER_ID => the token path is skipped entirely, so even
    an otherwise-valid token falls through to Clerk and fails."""
    client = _make_client(monkeypatch, "")
    tok = platform_ops_token.mint("org_founder")
    with _clerk_rejects():
        res = client.get("/api/v1/admin/system/health", headers=_auth(tok))
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Clerk fallback still works on the changed endpoints
# ---------------------------------------------------------------------------


def test_clerk_platform_admin_still_passes(client):
    ctx = AuthContext(user_id="user_admin", email="admin@isol8.co")
    with (
        patch("core.services.admin_access.get_current_user", AsyncMock(return_value=ctx)),
        patch("routers.admin.system_health.get_system_health", AsyncMock(return_value={"ok": True})),
        patch(
            "routers.admin.admin_service.list_users",
            AsyncMock(return_value={"users": [], "cursor": None, "stubbed": False}),
        ),
    ):
        health = client.get("/api/v1/admin/system/health", headers=_auth("a-clerk-jwt"))
        users = client.get("/api/v1/admin/users", headers=_auth("a-clerk-jwt"))
    assert health.status_code == 200
    assert users.status_code == 200


def test_clerk_non_admin_still_403(client):
    ctx = AuthContext(user_id="user_rando", email="user@example.com")
    with patch("core.services.admin_access.get_current_user", AsyncMock(return_value=ctx)):
        res = client.get("/api/v1/admin/system/health", headers=_auth("a-clerk-jwt"))
    assert res.status_code == 403
    assert res.json()["detail"] == "Platform admin access required"
