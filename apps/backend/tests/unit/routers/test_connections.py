"""Tests for the customer-facing connections router (Bucket A paste-key flow).

Mirrors the auth-override + async_client idiom from test_billing.py / conftest:
``get_current_user`` is overridden by the ``async_client`` fixture (personal
mode, owner_id == "user_test_123"). Async repo / apply calls are patched with
``AsyncMock`` so the awaits resolve.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestListConnections:
    """GET /api/v1/connections — catalog + per-provider status."""

    @pytest.mark.asyncio
    @patch("routers.connections.connection_repo")
    async def test_list_returns_catalog_with_status(self, mock_repo, async_client):
        # linear is connected; google has no row -> disconnected.
        mock_repo.list_for_owner = AsyncMock(return_value=[{"provider": "linear", "status": "connected"}])

        response = await async_client.get("/api/v1/connections")
        assert response.status_code == 200

        by_key = {c["provider"]: c for c in response.json()["connections"]}
        # Every registry provider is represented.
        assert {"linear", "stripe", "google"} <= set(by_key)
        assert by_key["linear"]["status"] == "connected"
        assert by_key["google"]["status"] == "disconnected"
        # Catalog metadata surfaced from the registry.
        assert by_key["linear"]["display_name"] == "Linear"
        assert by_key["linear"]["acquisition"] == "paste_token"
        assert by_key["google"]["acquisition"] == "direct_oauth"

        mock_repo.list_for_owner.assert_awaited_once_with("user_test_123")


class TestConnectPaste:
    """POST /api/v1/connections/{provider} — paste-key connect."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.connection_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_happy_path(self, mock_validate, mock_repo, mock_apply, async_client):
        mock_validate.return_value = True
        mock_repo.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.post("/api/v1/connections/linear", json={"token": "lin_api_abc123"})
        assert response.status_code == 200
        assert response.json() == {"status": "connected", "provider": "linear"}

        # Credential persisted with the pasted token.
        mock_repo.put.assert_awaited_once()
        put_args = mock_repo.put.await_args
        assert put_args.args[0] == "user_test_123"
        assert put_args.args[1] == "linear"
        assert put_args.args[2] == {"token": "lin_api_abc123"}

        # Wiring engine invoked with (owner, provider, credential).
        mock_apply.apply_connection.assert_awaited_once_with("user_test_123", "linear", {"token": "lin_api_abc123"})

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.connection_repo")
    @patch("routers.connections._validate_token", new_callable=AsyncMock)
    async def test_connect_paste_validation_failure_returns_422(
        self, mock_validate, mock_repo, mock_apply, async_client
    ):
        mock_validate.return_value = False
        mock_repo.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.post("/api/v1/connections/linear", json={"token": "bad"})
        assert response.status_code == 422
        # Nothing persisted / wired when validation fails.
        mock_repo.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_non_paste_provider_returns_400(self, async_client):
        # google is direct_oauth, not paste_token — must be rejected at this surface.
        response = await async_client.post("/api/v1/connections/google", json={"token": "anything"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_connect_unknown_provider_returns_404(self, async_client):
        response = await async_client.post("/api/v1/connections/does-not-exist", json={"token": "x"})
        assert response.status_code == 404


class TestDisconnect:
    """DELETE /api/v1/connections/{provider} — disconnect + unwire."""

    @pytest.mark.asyncio
    @patch("routers.connections.config_patcher")
    @patch("routers.connections.connection_repo")
    async def test_disconnect_deletes_and_blanks_openclaw_path(self, mock_repo, mock_patcher, async_client):
        mock_repo.delete = AsyncMock()
        mock_patcher.set_openclaw_config_path = AsyncMock()

        response = await async_client.delete("/api/v1/connections/linear")
        assert response.status_code == 200
        assert response.json() == {"status": "disconnected", "provider": "linear"}

        mock_repo.delete.assert_awaited_once_with("user_test_123", "linear")
        # linear wiring path: mcp.servers.linear.env.LINEAR_API_KEY -> blanked.
        mock_patcher.set_openclaw_config_path.assert_awaited_once_with(
            "user_test_123",
            ["mcp", "servers", "linear", "env", "LINEAR_API_KEY"],
            "",
        )

    @pytest.mark.asyncio
    async def test_disconnect_unknown_provider_returns_404(self, async_client):
        response = await async_client.delete("/api/v1/connections/does-not-exist")
        assert response.status_code == 404


class TestAuthorize:
    """GET /api/v1/connections/{provider}/authorize — start the OAuth dance."""

    @pytest.mark.asyncio
    @patch("routers.connections.oauth_dance")
    async def test_authorize_returns_consent_url(self, mock_dance, async_client):
        consent_url = "https://accounts.google.com/o/oauth2/v2/auth?x=1"
        mock_dance.start = AsyncMock(return_value=consent_url)

        response = await async_client.get("/api/v1/connections/google/authorize")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith(consent_url)

        # Dance started with the server-derived redirect_uri.
        mock_dance.start.assert_awaited_once()
        call = mock_dance.start.await_args
        assert call.args[0] == "user_test_123"
        assert call.args[1] == "google"
        assert call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")

    @pytest.mark.asyncio
    async def test_authorize_non_oauth_provider_returns_400(self, async_client):
        # linear is paste_token, not an OAuth integration.
        response = await async_client.get("/api/v1/connections/linear/authorize")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_authorize_unknown_provider_returns_404(self, async_client):
        response = await async_client.get("/api/v1/connections/does-not-exist/authorize")
        assert response.status_code == 404


class TestOAuthCallback:
    """GET /api/v1/oauth/{provider}/callback — PUBLIC; identity from state record."""

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.connection_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_happy_path_persists_and_redirects(self, mock_dance, mock_repo, mock_apply, async_client):
        cred = {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
        mock_dance.exchange = AsyncMock(return_value=("org_1", "google", cred))
        mock_repo.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "/settings/connections?connected=google" in response.headers["location"]

        # Identity + credential recovered from the state record.
        mock_dance.exchange.assert_awaited_once()
        ex_call = mock_dance.exchange.await_args
        assert ex_call.args[0] == "st1"
        assert ex_call.args[1] == "abc"
        assert ex_call.kwargs["redirect_uri"].endswith("/api/v1/oauth/google/callback")

        mock_repo.put.assert_awaited_once()
        mock_apply.apply_connection.assert_awaited_once_with("org_1", "google", cred)

    @pytest.mark.asyncio
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.connection_repo")
    @patch("routers.connections.oauth_dance")
    async def test_callback_bad_state_redirects_with_error_and_no_writes(
        self, mock_dance, mock_repo, mock_apply, async_client
    ):
        mock_dance.exchange = AsyncMock(side_effect=ValueError("invalid or expired OAuth state"))
        mock_repo.put = AsyncMock()
        mock_apply.apply_connection = AsyncMock()

        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=bad",
            follow_redirects=False,
        )
        assert response.status_code in (302, 307)
        assert "error=oauth_state" in response.headers["location"]

        # Nothing persisted / wired when state validation fails.
        mock_repo.put.assert_not_called()
        mock_apply.apply_connection.assert_not_called()
