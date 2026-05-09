"""Integration tests for /api/v1/oauth/chatgpt/* endpoints."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.oauth_service import (
    DeviceCodeResponse,
    DevicePollPending,
    DevicePollResult,
    OAuthAlreadyActiveError,
)


@pytest.mark.asyncio
async def test_start_returns_user_code_and_verification_uri(async_client):
    fake_resp = DeviceCodeResponse(
        user_code="TEST-1234",
        verification_uri="https://chatgpt.com/codex",
        expires_in=900,
        interval=5,
    )
    with patch(
        "routers.oauth.request_device_code",
        new=AsyncMock(return_value=fake_resp),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_code"] == "TEST-1234"
    assert body["verification_uri"] == "https://chatgpt.com/codex"
    assert body["interval"] == 5
    assert body["expires_in"] == 900


@pytest.mark.asyncio
async def test_start_returns_409_when_already_active(async_client):
    """When user already has an active OAuth session, /start returns 409."""
    with patch(
        "routers.oauth.request_device_code",
        new=AsyncMock(side_effect=OAuthAlreadyActiveError("already active")),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_poll_returns_pending_status(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(return_value=DevicePollPending),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


@pytest.mark.asyncio
async def test_poll_returns_completed_status(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(return_value=DevicePollResult(account_id="acc_1")),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "completed", "account_id": "acc_1"}


@pytest.mark.asyncio
async def test_disconnect_revokes(async_client):
    with patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke:
        resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
    assert resp.status_code == 200
    mock_revoke.assert_awaited_once()


class TestStartProviderRules:
    """POST /api/v1/oauth/chatgpt/start — org/personal context rules.

    Per memory/project_chatgpt_oauth_personal_only.md (decision 2026-04-30),
    ChatGPT OAuth is allowed only for personal/single-user workspaces. This
    endpoint must reject org-context callers with 403 before any device-code
    request, as belt-and-suspenders to the trial-checkout block: a future
    "reconnect" flow that calls /start outside trial-checkout still won't
    bypass the rule.
    """

    @pytest.fixture
    def override_org_admin_auth(self, app, mock_org_admin_user):
        """Swap get_current_user with an org-admin AuthContext for one test."""
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        yield
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_start_rejects_org_context(self, async_client, override_org_admin_auth):
        """Org-context user calling /oauth/chatgpt/start gets 403 before any device-code request."""
        fake_resp = DeviceCodeResponse(
            user_code="TEST-1234",
            verification_uri="https://chatgpt.com/codex",
            expires_in=900,
            interval=5,
        )
        with patch(
            "routers.oauth.request_device_code",
            new=AsyncMock(return_value=fake_resp),
        ) as mock_request:
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 403
        assert "organization" in resp.json()["detail"].lower()
        mock_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_allows_personal_context(self, async_client):
        """Personal user (no org_id) still reaches the device-code request."""
        # Default async_client uses mock_current_user — personal mode (no org_id).
        fake_resp = DeviceCodeResponse(
            user_code="TEST-1234",
            verification_uri="https://chatgpt.com/codex",
            expires_in=900,
            interval=5,
        )
        with patch(
            "routers.oauth.request_device_code",
            new=AsyncMock(return_value=fake_resp),
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 200


class TestStatus:
    """GET /api/v1/oauth/chatgpt/status — wizard idempotency probe.

    Used by useChatGPTOAuth on mount so the wizard can recover from a
    remount mid-flow (e.g. tab close + revisit, or in-tab parent re-render
    that unmounts + remounts the OAuth step). Strongly-consistent DDB read
    so the wizard reads its own write from /poll without an
    eventual-consistency stutter.
    """

    @pytest.fixture
    def override_org_admin_auth(self, app, mock_org_admin_user):
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        yield
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_status_returns_connected_true_when_active_row_exists(self, async_client):
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(
                return_value={
                    "account_id": "acc_x",
                    "access_token": "tok-a",
                    "refresh_token": "tok-r",
                }
            ),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": "acc_x"}

    @pytest.mark.asyncio
    async def test_status_returns_connected_false_when_no_row(self, async_client):
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(return_value=None),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": False, "account_id": None}

    @pytest.mark.asyncio
    async def test_status_returns_connected_true_when_account_id_missing(self, async_client):
        # Older rows may not have account_id (extracted from JWT post-2026-04).
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(return_value={"access_token": "tok-a", "refresh_token": "tok-r"}),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": None}

    @pytest.mark.asyncio
    async def test_status_rejects_org_context(self, async_client, override_org_admin_auth):
        """Org-context user calling /status gets 403 before any DDB read.

        Mirrors /start's org guard. Even though /status is read-only,
        ChatGPT OAuth is personal-only per
        memory/project_chatgpt_oauth_personal_only.md.
        """
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(return_value=None),
        ) as mock_lookup:
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 403
        assert "organization" in resp.json()["detail"].lower()
        mock_lookup.assert_not_awaited()
