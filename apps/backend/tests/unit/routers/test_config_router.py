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
    return AuthContext(user_id=user_id)


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


def _mock_billing(status: str | None):
    """Mock the billing-account lookup. Pass `None` for the pre-signup case
    (no billing row); pass "active"/"trialing"/"past_due"/"canceled" to
    simulate the corresponding subscription state."""
    return patch(
        "routers.config.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


def test_patch_config_personal_user_succeeds(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch, _mock_billing("active"):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"channels": {"telegram": {"enabled": True}}}},
            )
        assert resp.status_code == 200
        mock_patch.assert_awaited_once()
        call_args = mock_patch.call_args
        assert call_args[0][0] == "user_personal"
    finally:
        cleanup()


def test_patch_config_org_admin_succeeds(client):
    cleanup = _patch_auth(_org_admin_auth())
    try:
        with patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch, _mock_billing("active"):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"channels": {"telegram": {"enabled": True}}}},
            )
        assert resp.status_code == 200
        mock_patch.assert_awaited_once()
        assert mock_patch.call_args[0][0] == "org_1"
    finally:
        cleanup()


def test_patch_config_org_member_rejected(client):
    cleanup = _patch_auth(_org_member_auth())
    try:
        with patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch, _mock_billing("active"):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"channels": {"telegram": {"enabled": True}}}},
            )
        assert resp.status_code == 403
        mock_patch.assert_not_called()
    finally:
        cleanup()


def test_patch_config_no_subscription_channels_rejected(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch, _mock_billing(None):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"channels": {"telegram": {"enabled": True}}}},
            )
        assert resp.status_code == 403
        assert "channels_require_subscription" in resp.json().get("detail", "")
        mock_patch.assert_not_called()
    finally:
        cleanup()


def test_patch_config_no_subscription_non_channels_succeeds(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch, _mock_billing(None):
            resp = client.patch(
                "/api/v1/config",
                json={"patch": {"tools": {"profile": "full"}}},
            )
        assert resp.status_code == 200
        mock_patch.assert_awaited_once()
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


def test_patch_config_rejects_token_collision(client):
    """Pasting a token already assigned to a different agent returns 409."""
    cleanup = _patch_auth(_personal_auth())
    try:
        existing_cfg = {
            "channels": {
                "telegram": {
                    "accounts": {
                        "main": {"botToken": "SHARED_TOKEN"},
                    },
                },
            },
        }
        with (
            patch("routers.config.patch_openclaw_config", AsyncMock()),
            patch(
                "routers.config.read_openclaw_config_from_efs",
                AsyncMock(return_value=existing_cfg),
            ),
            _mock_billing("active"),
        ):
            resp = client.patch(
                "/api/v1/config",
                json={
                    "patch": {
                        "channels": {
                            "telegram": {
                                "accounts": {
                                    "sales": {"botToken": "SHARED_TOKEN"},
                                },
                            },
                        },
                    },
                },
            )
        assert resp.status_code == 409
        assert "token_already_assigned_to_other_agent" in resp.json().get("detail", "")
    finally:
        cleanup()


def test_patch_config_allows_overwriting_own_agent_token(client):
    """Updating the SAME agent's token is fine (overwrite)."""
    cleanup = _patch_auth(_personal_auth())
    try:
        existing_cfg = {
            "channels": {
                "telegram": {
                    "accounts": {
                        "main": {"botToken": "OLD_TOKEN"},
                    },
                },
            },
        }
        with (
            patch("routers.config.patch_openclaw_config", AsyncMock()) as mock_patch,
            patch(
                "routers.config.read_openclaw_config_from_efs",
                AsyncMock(return_value=existing_cfg),
            ),
            _mock_billing("active"),
        ):
            resp = client.patch(
                "/api/v1/config",
                json={
                    "patch": {
                        "channels": {
                            "telegram": {
                                "accounts": {
                                    "main": {"botToken": "NEW_TOKEN"},
                                },
                            },
                        },
                    },
                },
            )
        assert resp.status_code == 200
        mock_patch.assert_awaited_once()
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# GET /api/v1/config
# ---------------------------------------------------------------------------


def test_get_config_personal_user_returns_dict(client):
    """Personal callers read their own user-scoped openclaw.json."""
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch(
            "routers.config.read_openclaw_config_from_efs",
            AsyncMock(return_value={"identity": {"name": "main"}}),
        ) as mock_read:
            resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner_id"] == "user_personal"
        assert body["config"] == {"identity": {"name": "main"}}
        mock_read.assert_awaited_once_with("user_personal")
    finally:
        cleanup()


def test_get_config_org_admin_resolves_to_org_id(client):
    """Org callers read org-scoped config (owner_id is the org_id, not the user)."""
    cleanup = _patch_auth(_org_admin_auth())
    try:
        with patch(
            "routers.config.read_openclaw_config_from_efs",
            AsyncMock(return_value={"agents": {"list": []}}),
        ) as mock_read:
            resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner_id"] == "org_1"
        mock_read.assert_awaited_once_with("org_1")
    finally:
        cleanup()


def test_get_config_org_member_rejected(client):
    """Non-admin org members can't read the shared org config — it carries
    secrets (channel botTokens, MCP env, etc.) only the admin set up."""
    cleanup = _patch_auth(_org_member_auth())
    try:
        with patch(
            "routers.config.read_openclaw_config_from_efs",
            AsyncMock(return_value={}),
        ) as mock_read:
            resp = client.get("/api/v1/config")
        assert resp.status_code == 403
        # Read should never have been attempted.
        mock_read.assert_not_awaited()
    finally:
        cleanup()


def test_get_config_returns_null_when_missing(client):
    """File missing on EFS (e.g., mid-provisioning) → config: null."""
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch(
            "routers.config.read_openclaw_config_from_efs",
            AsyncMock(return_value=None),
        ):
            resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"] is None
    finally:
        cleanup()
