"""
Unit tests for WebSocket HTTP routes (API Gateway integration).

Tests the HTTP routes that handle API Gateway WebSocket events:
- POST /ws/connect - Handle $connect event
- POST /ws/disconnect - Handle $disconnect event
- POST /ws/message - Handle $default (message) events

These tests use httpx AsyncClient with ASGITransport for testing HTTP endpoints.
Services are mocked to isolate route logic.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.websocket_chat import router


@pytest.fixture
def test_app():
    """Create a test FastAPI app with the websocket router."""
    app = FastAPI()
    app.include_router(router, prefix="/ws")
    return app


@pytest.fixture
def mock_connection_service():
    """Mock ConnectionService for testing."""
    with patch("routers.websocket_chat.get_connection_service") as mock_getter:
        mock_service = MagicMock()
        mock_getter.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_management_api():
    """Mock ManagementApiClient for testing."""
    with patch("routers.websocket_chat.get_management_api_client") as mock_getter:
        mock_client = MagicMock()
        mock_client.send_message = MagicMock(return_value=True)
        mock_getter.return_value = mock_client
        yield mock_client


@pytest.fixture(autouse=True)
def mock_gateway_pool():
    """Mock gateway connection pool (autouse since it's called in connect/disconnect)."""
    with patch("routers.websocket_chat.get_gateway_pool") as mock_getter:
        mock_pool = MagicMock()
        mock_getter.return_value = mock_pool
        yield mock_pool


class TestConnectEndpoint:
    """Tests for POST /ws/connect endpoint."""

    @pytest.mark.asyncio
    async def test_connect_stores_connection(self, test_app, mock_connection_service):
        """Connect should store connection in DynamoDB with type=chat by default."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-123",
                    "x-user-id": "test-user-456",
                },
            )

        assert response.status_code == 200
        mock_connection_service.store_connection.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            org_id=None,
            connection_type="chat",
        )

    @pytest.mark.asyncio
    async def test_connect_stores_connection_with_org_id(self, test_app, mock_connection_service):
        """Connect should store org_id when provided."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-123",
                    "x-user-id": "test-user-456",
                    "x-org-id": "test-org-789",
                },
            )

        assert response.status_code == 200
        mock_connection_service.store_connection.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            org_id="test-org-789",
            connection_type="chat",
        )

    @pytest.mark.asyncio
    async def test_connect_marks_node_host_clients_as_node_type(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """node-host clientIds should be stored with connection_type='node'.

        Regression: ws_connect kicks off `_kick_off_node_connect` for
        node-host clients (no proxy-injected wake-up frame), so the
        connection MUST be flagged 'node' at $connect time, not later
        when the first frame arrives.
        """
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-node-1",
                    "x-user-id": "test-user-xyz",
                    "x-client-id": "node-host",
                },
            )

        assert response.status_code == 200
        mock_connection_service.store_connection.assert_called_once_with(
            connection_id="test-conn-node-1",
            user_id="test-user-xyz",
            org_id=None,
            connection_type="node",
        )

    @pytest.mark.asyncio
    async def test_connect_without_connection_id_fails(self, test_app, mock_connection_service):
        """Connect without x-connection-id header should return 400."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-user-id": "test-user-456",
                },
            )

        assert response.status_code == 400
        assert "connection-id" in response.json()["detail"].lower()
        mock_connection_service.store_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_without_user_id_fails(self, test_app, mock_connection_service):
        """Connect without x-user-id header should return 401."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-123",
                },
            )

        assert response.status_code == 401
        assert "user-id" in response.json()["detail"].lower()
        mock_connection_service.store_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_skips_synthetic_challenge_for_node_host(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """Connect should skip the synthetic connect.challenge for node-host clients.

        The Tauri proxy is doing client-first sequencing; the real
        connect.challenge will come from the upstream container via
        handle_node_connect's relay path. Sending a synthetic nonce here
        would race that and could cause the subprocess to sign the wrong
        nonce.
        """
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-node",
                    "x-user-id": "test-user-456",
                    "x-client-id": "node-host",
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_skips_synthetic_challenge_for_openclaw_macos(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """Connect should skip the synthetic connect.challenge for openclaw-macos clients."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-mac",
                    "x-user-id": "test-user-456",
                    "x-client-id": "openclaw-macos",
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_sends_synthetic_challenge_for_default_client(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """Connect should send synthetic connect.challenge when no clientId header set.

        Browser chat/control UI clients don't set x-client-id; they
        depend on the synthetic challenge to complete their handshake.
        """
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-browser",
                    "x-user-id": "test-user-456",
                },
            )

        assert response.status_code == 200
        # Synthetic challenge fires via BackgroundTask after the response
        # is returned -- TestClient/ASGITransport awaits background tasks
        # before the context exits, so by here send_message has been called.
        mock_management_api.send_message.assert_called_once()
        call_args = mock_management_api.send_message.call_args
        assert call_args[0][0] == "test-conn-browser"
        payload = call_args[0][1]
        assert payload["event"] == "connect.challenge"
        assert "nonce" in payload["payload"]

    @pytest.mark.asyncio
    async def test_connect_sends_synthetic_challenge_for_unknown_client_id(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """An unrecognized clientId should still get the synthetic challenge (default behavior)."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/connect",
                headers={
                    "x-connection-id": "test-conn-other",
                    "x-user-id": "test-user-456",
                    "x-client-id": "some-other-client",
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()


class TestDisconnectEndpoint:
    """Tests for POST /ws/disconnect endpoint."""

    @pytest.mark.asyncio
    async def test_disconnect_deletes_connection(self, test_app, mock_connection_service):
        """Disconnect should delete connection from DynamoDB."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/disconnect",
                headers={
                    "x-connection-id": "test-conn-123",
                },
            )

        assert response.status_code == 200
        mock_connection_service.delete_connection.assert_called_once_with("test-conn-123")

    @pytest.mark.asyncio
    async def test_disconnect_without_connection_id_returns_200(self, test_app, mock_connection_service):
        """Disconnect without connection-id should still return 200 (best effort)."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/disconnect",
                headers={},
            )

        # Best effort cleanup - always returns 200
        assert response.status_code == 200
        mock_connection_service.delete_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_handles_service_error(self, test_app, mock_connection_service):
        """Disconnect should return 200 even if service throws error (best effort)."""
        from core.services.connection_service import ConnectionServiceError

        mock_connection_service.delete_connection.side_effect = ConnectionServiceError("DynamoDB error")

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/disconnect",
                headers={
                    "x-connection-id": "test-conn-123",
                },
            )

        # Best effort cleanup - always returns 200
        assert response.status_code == 200


class TestMessageEndpoint:
    """Tests for POST /ws/message endpoint."""

    @pytest.mark.asyncio
    async def test_message_looks_up_connection(self, test_app, mock_connection_service, mock_management_api):
        """Message should look up connection to get user context."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user-456",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "test-conn-123",
                },
                json={"type": "ping"},
            )

        assert response.status_code == 200
        mock_connection_service.get_connection.assert_called_once_with("test-conn-123")

    @pytest.mark.asyncio
    async def test_message_handles_ping(self, test_app, mock_connection_service, mock_management_api):
        """Ping message should respond with pong via Management API."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user-456",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "test-conn-123",
                },
                json={"type": "ping"},
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once_with(
            "test-conn-123",
            {"type": "pong"},
        )

    @pytest.mark.asyncio
    async def test_message_lazy_registers_frontend_connection(
        self,
        test_app,
        mock_connection_service,
        mock_management_api,
        mock_gateway_pool,
    ):
        """Regression: backend tasks rotate (deploys, scaling) but the
        client's WS stays open via API Gateway. /ws/connect only fires
        once on initial WS open — subsequent /ws/message calls land on
        whichever backend task ALB picks. Without lazy registration in
        /ws/message, the new task's pool has empty `_frontend_connections`
        and openclaw streaming events get silently dropped, leaving the
        chat UI stuck loading.
        """
        mock_connection_service.get_connection.return_value = {
            "user_id": "user_3D8qdjOCXakKFHCsErQHS7q8P9v",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "ctMiYcLWoAMCLgg="},
                # `pong` is a non-ping path that exits early without
                # touching openclaw; the lazy-register block runs before
                # that check, so this is sufficient to verify the call.
                json={"type": "pong"},
            )

        assert response.status_code == 200
        mock_gateway_pool.add_frontend_connection.assert_called_once_with(
            "user_3D8qdjOCXakKFHCsErQHS7q8P9v",
            "ctMiYcLWoAMCLgg=",
            member_id="user_3D8qdjOCXakKFHCsErQHS7q8P9v",
        )

    @pytest.mark.asyncio
    async def test_message_does_not_register_on_ping(
        self,
        test_app,
        mock_connection_service,
        mock_management_api,
        mock_gateway_pool,
    ):
        """Ping is the high-frequency keepalive path — registering on
        every ping would be wasted work. Verify the lazy-register call
        happens only on non-ping messages.
        """
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "test-conn"},
                json={"type": "ping"},
            )

        assert response.status_code == 200
        mock_gateway_pool.add_frontend_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_rejects_unknown_connection(self, test_app, mock_connection_service, mock_management_api):
        """Message with unknown connection should return 401."""
        mock_connection_service.get_connection.return_value = None

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "unknown-conn",
                },
                json={"type": "ping"},
            )

        assert response.status_code == 401
        assert "unknown connection" in response.json()["detail"].lower()
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_without_connection_id_fails(self, test_app, mock_connection_service, mock_management_api):
        """Message without x-connection-id should return 400."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={},
                json={"type": "ping"},
            )

        assert response.status_code == 400
        assert "connection-id" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_message_handles_pong_silently(self, test_app, mock_connection_service, mock_management_api):
        """Pong messages should be acknowledged silently (no response sent)."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user-456",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "test-conn-123",
                },
                json={"type": "pong"},
            )

        assert response.status_code == 200
        # Pong is a client ack - no response needed
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_message_type_sends_error(self, test_app, mock_connection_service, mock_management_api):
        """Unknown message type should send error via Management API."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user-456",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "test-conn-123",
                },
                json={"type": "invalid_type"},
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()
        call_args = mock_management_api.send_message.call_args
        assert call_args[0][0] == "test-conn-123"
        assert call_args[0][1]["type"] == "error"
        assert "unknown message type" in call_args[0][1]["message"].lower()


class TestReqMessageRouting:
    """Tests for type=req RPC proxy messages."""

    @pytest.mark.asyncio
    async def test_req_message_accepted(self, test_app, mock_connection_service, mock_management_api):
        """Valid req message should be accepted and return 200."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        with patch("routers.websocket_chat._process_rpc_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "conn-123"},
                    json={
                        "type": "req",
                        "id": "req-uuid-1",
                        "method": "health",
                        "params": {},
                    },
                )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()
        mock_bg.assert_called_once()

    @pytest.mark.asyncio
    async def test_req_background_task_receives_correct_args(
        self, test_app, mock_connection_service, mock_management_api
    ):
        """Background task should receive connection_id, user_id, req_id, method, params."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        with patch("routers.websocket_chat._process_rpc_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "conn-123"},
                    json={
                        "type": "req",
                        "id": "req-uuid-1",
                        "method": "agents.list",
                        "params": {"filter": "active"},
                    },
                )

        mock_bg.assert_called_once_with(
            connection_id="conn-123",
            user_id="test-user",
            owner_id="test-user",
            req_id="req-uuid-1",
            method="agents.list",
            params={"filter": "active"},
        )

    @pytest.mark.asyncio
    async def test_req_missing_id_sends_error(self, test_app, mock_connection_service, mock_management_api):
        """req without id should send error res back."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-123"},
                json={"type": "req", "method": "health"},
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()
        sent_msg = mock_management_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "res"
        assert sent_msg["ok"] is False

    @pytest.mark.asyncio
    async def test_req_missing_method_sends_error(self, test_app, mock_connection_service, mock_management_api):
        """req without method should send error res back."""
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-123"},
                json={"type": "req", "id": "req-uuid-2"},
            )

        assert response.status_code == 200
        sent_msg = mock_management_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "res"
        assert sent_msg["id"] == "req-uuid-2"
        assert sent_msg["ok"] is False


class TestUserActiveLegacy:
    """The user_active heartbeat used to drive scale-to-zero idle detection;
    post flat-fee cutover the path is a no-op 200 — we only keep the test to
    confirm the endpoint doesn't 4xx if a stale client still sends the ping."""

    @pytest.mark.asyncio
    async def test_user_active_returns_200_no_op(self, test_app, mock_connection_service, mock_management_api):
        mock_connection_service.get_connection.return_value = {
            "user_id": "test-user",
            "org_id": None,
        }

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-abc"},
                json={"type": "user_active"},
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()


class TestSessionBindsToCliDevice:
    """agent_chat session-binding must pick the CLI device specifically
    (not whichever happened to be set last). The exec router
    only knows how to invoke against the CLI bucket; pinning execNode to
    the Mac-companion's deviceId would silently break exec.

    These are contract tests rather than full handler tests — the
    message-handler is too tangled with provision_gate, billing, and
    container resolution to exercise cleanly here. We validate the two
    contract pieces directly:
      1. `get_user_cli_node(user_id)["deviceId"]` is what session.patch
         must send as `execNode`, even when a Mac bucket also exists.
      2. The session-binding callsite imports `get_user_cli_node` (not
         the deprecated `get_user_node`) from node_proxy.
    """

    def test_get_user_cli_node_returns_cli_deviceid_when_both_buckets_present(self):
        """Mac connected LATER (chronologically); cli still wins for exec."""
        from core.services import node_proxy
        from core.services.node_proxy import get_user_cli_node

        node_proxy._user_nodes.clear()
        try:
            node_proxy._user_nodes["user_alice"] = {
                "cli": {
                    "deviceId": "dev_cli_target",
                    "clientId": "node-host",
                    "connection_id": "c_cli",
                    "owner_id": "o",
                },
                "mac": {
                    "deviceId": "dev_mac_other",
                    "clientId": "openclaw-macos",
                    "connection_id": "c_mac",
                    "owner_id": "o",
                },
            }

            cli = get_user_cli_node("user_alice")
            assert cli is not None
            # This is the value the session-binding callsite will pass as
            # execNode. Asserting it directly is the contract we care about.
            assert cli["deviceId"] == "dev_cli_target"
            # And the field is `deviceId`, NOT the transient `nodeId` alias.
            assert "nodeId" not in cli or cli.get("nodeId") is None
        finally:
            node_proxy._user_nodes.clear()

    def test_websocket_chat_imports_get_user_cli_node(self):
        """Callsite must import get_user_cli_node, not the deprecated
        get_user_node."""
        import inspect

        from routers import websocket_chat

        src = inspect.getsource(websocket_chat)
        # The session-binding block reads the CLI device specifically.
        assert "get_user_cli_node" in src, (
            "websocket_chat.py must call get_user_cli_node for "
            "session-binding; the deprecated get_user_node is wrong here."
        )
        # And it reads deviceId, not the transient nodeId alias.
        assert 'node_info["deviceId"]' in src or "node_info['deviceId']" in src, (
            "session-binding must read deviceId from the cli-node entry, not the transient nodeId alias."
        )
