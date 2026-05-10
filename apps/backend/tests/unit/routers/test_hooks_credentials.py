"""Tests for GET /api/v1/hooks/credentials (Clerk-authed)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_user(monkeypatch):
    # Standard pattern: override get_current_user dep for this test.
    from core.auth import get_current_user

    async def _fake_user():
        return AuthContext(user_id="u_1", org_id=None, email="t@x.co")

    app.dependency_overrides[get_current_user] = _fake_user
    yield
    app.dependency_overrides.clear()


def test_credentials_returns_url_and_token(client, auth_user):
    with patch(
        "routers.hooks_credentials.container_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "u_1",
                "hook_token": "tok",
                "hook_token_rotated_at": "2026-05-09T12:00:00Z",
            }
        ),
    ):
        r = client.get("/api/v1/hooks/credentials")
        assert r.status_code == 200
        data = r.json()
        # Note: the URL uses whatever PUBLIC_API_BASE is set to in the
        # test environment. The default in core/config.py is
        # "http://localhost:8000", so the assertion should match that.
        # If PUBLIC_API_BASE is overridden via .env in the test environment,
        # adapt this assertion accordingly. The contract is: it ends in
        # `/api/v1/hooks/u/u_1/`.
        assert data["public_url"].endswith("/api/v1/hooks/u/u_1/")
        assert data["token"] == "tok"
        assert data["rotated_at"] == "2026-05-09T12:00:00Z"


def test_credentials_404_if_not_provisioned(client, auth_user):
    with patch(
        "routers.hooks_credentials.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        r = client.get("/api/v1/hooks/credentials")
        assert r.status_code == 404


def test_credentials_409_when_hook_token_pending(client, auth_user):
    """Legacy row exists (provisioned before hook-token landed) but no
    hook_token field yet. Returning 200 with token="" would render an
    empty masked field in the Settings UI and silently copy a bad token
    into Stripe etc. — surface the pending state as 409 instead."""
    with patch(
        "routers.hooks_credentials.container_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "u_1",
                # No hook_token / hook_token_rotated_at — pre-T2 legacy row.
            }
        ),
    ):
        r = client.get("/api/v1/hooks/credentials")
        assert r.status_code == 409
        assert r.json()["detail"] == "hook_token_pending"
