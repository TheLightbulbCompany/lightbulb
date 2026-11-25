"""Tests for GET /api/v1/admin/me, the admin console's auth probe.

Auth model: `require_platform_admin` (see core/auth.py) gates every endpoint by
checking the caller's Clerk email against the platform-admin allowlist. Tests
toggle access by overriding `get_current_user` with an admitted or denied
AuthContext while leaving the real platform-admin gate in place.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, get_current_user

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# Helpers — local to this file; present the AuthContext (email claim) that
# require_platform_admin needs.
# ---------------------------------------------------------------------------


def _admit_test_user(app, email: str = "admin@isol8.co"):
    """Override auth so the caller looks like an Isol8 team member."""
    ctx = AuthContext(user_id="user_platform_admin", email=email, human_session=True)
    app.dependency_overrides[get_current_user] = lambda: ctx


def _deny_all_admins(app):
    """Override auth so the caller is not on the admin allowlist => 403."""
    ctx = AuthContext(user_id="user_test_123", email="user@example.com", human_session=True)
    app.dependency_overrides[get_current_user] = lambda: ctx


# ---------------------------------------------------------------------------
# Auth gate (5 cases) — every admin endpoint must hide behind require_platform_admin
# ---------------------------------------------------------------------------


class TestAdminAuthGate:
    """The /admin/* surface returns 403 unless the caller.s Clerk
    user id is on PLATFORM_ADMIN_USER_IDS."""

    @pytest.mark.asyncio
    async def test_admin_me_403_when_user_not_in_allowlist(self, async_client, app):
        """External email => 403 with the require_platform_admin detail string."""
        _deny_all_admins(app)
        response = await async_client.get("/api/v1/admin/me")
        assert response.status_code == 403
        assert response.json()["detail"] == "Platform admin access required"

    @pytest.mark.asyncio
    async def test_admin_me_200_for_allowlisted_user(self, async_client, app):
        """Allowlisted user id => 200 with profile."""
        _admit_test_user(app)
        response = await async_client.get("/api/v1/admin/me")
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_platform_admin"
        assert body["is_admin"] is True
        # `email` key is always present in the response shape (may be None when
        # the AuthContext didn't carry an email claim).
        assert "email" in body

    @pytest.mark.asyncio
    async def test_admin_me_rejects_missing_credentials(self, unauthenticated_async_client):
        response = await unauthenticated_async_client.get("/api/v1/admin/me")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_me_rejects_retired_agent_token(self, unauthenticated_async_client):
        import jwt

        with patch(
            "core.auth._decode_token",
            AsyncMock(side_effect=jwt.InvalidTokenError("not a Clerk JWT")),
        ):
            response = await unauthenticated_async_client.get(
                "/api/v1/admin/me",
                headers={"Authorization": "Bearer retired-agent-token"},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_me_includes_email_when_present(self, app):
        """When the JWT carried an email claim, /me echoes it back so the
        operator UI can show 'logged in as <email>'."""
        from httpx import ASGITransport, AsyncClient

        _admit_test_user(app, email="admin@isol8.co")
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/v1/admin/me")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["email"] == "admin@isol8.co"
