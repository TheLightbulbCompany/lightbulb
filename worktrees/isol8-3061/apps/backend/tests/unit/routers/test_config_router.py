"""Tests for GET + PATCH /api/v1/config router."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext  # noqa: E402


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _personal_auth(user_id: str = "user_personal") -> AuthContext:
    return AuthContext(user_id=f"human_{user_id}", org_id=user_id, org_role="org:admin")


def _org_admin_auth(user_id: str = "user_admin", org_id: str = "org_1") -> AuthContext:
    return AuthContext(user_id=user_id, org_id=org_id, org_role="org:admin")


def _org_member_auth(user_id: str = "user_member", org_id: str = "org_1") -> AuthContext:
    return AuthContext(user_id=user_id, org_id=org_id, org_role="org:member")


def _patch_auth(auth: AuthContext):
    """Override the FastAPI dependency that returns the auth context."""
    from core.auth import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = lambda: auth
    return lambda: app.dependency_overrides.pop(get_current_user, None)


def test_patch_config_org_member_rejected(client):
    cleanup = _patch_auth(_org_member_auth())
    try:
        with patch("routers.config.config_patch_roster", AsyncMock()) as mock_patch:
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"tools": {"profile": "full"}}},
            )
        assert resp.status_code == 403
        mock_patch.assert_not_called()
    finally:
        cleanup()


def test_patch_config_validation_rejects_non_dict_patch(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        resp = client.patch(
            "/api/v1/config",
            json={"patch": "not a dict"},
        )
        assert resp.status_code == 422  # Pydantic rejects
    finally:
        cleanup()


def test_get_config_org_member_rejected(client):
    """Non-admin org members can't read the shared org config — it carries
    secrets (MCP env, etc.) only the admin set up."""
    cleanup = _patch_auth(_org_member_auth())
    try:
        with patch(
            "routers.config._config_get_roster_map", AsyncMock(return_value={"config": {}, "hash": "h"})
        ) as read:
            resp = client.get("/api/v1/config")
        assert resp.status_code == 403
        # Read should never have been attempted.
        read.assert_not_awaited()
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# M6 row #12 — served from the S3 profile roster map via the SAME
# _config_get_roster_map/config_patch_roster the config.get/config.patch RPC
# intercept uses, so the raw editor and the settings panel cannot disagree.
# ---------------------------------------------------------------------------


def test_get_config_reads_the_roster_map(client):
    cleanup = _patch_auth(_org_admin_auth())
    try:
        with (
            patch(
                "routers.config._config_get_roster_map",
                AsyncMock(return_value={"config": {"agent-1": {"model": {"default": "x"}}}, "hash": "h1"}),
            ) as mock_roster,
        ):
            resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner_id"] == "org_1"
        assert body["config"] == {"agent-1": {"model": {"default": "x"}}}
        assert body["hash"] == "h1"
        mock_roster.assert_awaited_once_with("org_1")
    finally:
        cleanup()


def test_patch_config_fans_out_via_config_patch_roster(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with (
            patch(
                "routers.config.config_patch_roster",
                AsyncMock(return_value={"config": {}, "hash": "h2"}),
            ) as mock_patch_roster,
        ):
            resp = client.patch("/api/v1/config", json={"patch": {"tools": {"profile": "full"}}})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "patched", "owner_id": "user_personal", "hash": "h2"}
        mock_patch_roster.assert_awaited_once_with("user_personal", {"tools": {"profile": "full"}}, None)
    finally:
        cleanup()


def test_patch_config_channels_patch_is_rejected(client):
    """Channels have no Hermes equivalent yet, so this must be a flat 400 rather than a
    roster-profile write of inert `channels.*` junk."""
    cleanup = _patch_auth(_personal_auth())
    try:
        with (
            patch("routers.config.config_patch_roster", AsyncMock()) as mock_patch_roster,
        ):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"channels": {"telegram": {"enabled": True}}}},
            )
        assert resp.status_code == 400
        assert "INVALID_REQUEST" in resp.json()["detail"]
        mock_patch_roster.assert_not_awaited()
    finally:
        cleanup()
