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
