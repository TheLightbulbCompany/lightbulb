"""
Unit tests for WebSocket agent_chat message type routing and validation.

Tests the agent_chat message type in POST /ws/message:
- Routing: agent_chat messages are accepted and background task is queued
- Validation: missing fields send errors via Management API
- Background task: uses chat.send RPC via GatewayConnectionPool

Uses the same pattern as test_websocket_chat.py:
- httpx AsyncClient with ASGITransport for HTTP endpoint testing
- Mocked ConnectionService and ManagementApiClient to isolate route logic
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.websocket_chat import router, _process_agent_chat_background


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


@pytest.fixture
def valid_agent_chat_message():
    """Create a valid agent_chat message payload (plaintext)."""
    return {
        "type": "agent_chat",
        "agent_id": "my-agent",
        "message": "Hello, agent!",
    }


@pytest.fixture
def connected_user(mock_connection_service):
    """Set up mock connection service to return a connected user."""
    mock_connection_service.get_connection.return_value = {
        "user_id": "test-user-456",
        "org_id": None,
    }
    return "test-user-456"


@pytest.fixture
def mock_check_budget_allowed():
    """No-op fixture retained for test arg compatibility — the per-tier budget
    pre-gate was deleted in the flat-fee cutover (gating now happens in the
    gateway connection_pool.gate_chat for card-3 users)."""
    yield None


class TestAgentChatMessageRouting:
    """Tests that the ws_message endpoint correctly routes agent_chat messages."""

    @pytest.mark.asyncio
    async def test_agent_chat_message_accepted(
        self,
        test_app,
        mock_connection_service,
        mock_management_api,
        valid_agent_chat_message,
        connected_user,
        mock_check_budget_allowed,
    ):
        """Send valid agent_chat message, verify 200 response.

        A valid agent_chat message should be accepted and return 200.
        The background task is queued but not awaited in the HTTP handler.
        """
        with patch("routers.websocket_chat._process_agent_chat_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json=valid_agent_chat_message,
                )

        assert response.status_code == 200
        # No error should be sent via Management API for a valid message
        mock_management_api.send_message.assert_not_called()
        # Background task should have been queued (called by BackgroundTasks)
        mock_bg.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_chat_missing_agent_id_sends_error(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """Missing agent_id field should send error via Management API."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "test-conn-123"},
                json={
                    "type": "agent_chat",
                    "message": "Hello!",
                    # agent_id missing
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()
        call_args = mock_management_api.send_message.call_args
        assert call_args[0][0] == "test-conn-123"
        assert call_args[0][1]["type"] == "error"

    @pytest.mark.asyncio
    async def test_agent_chat_missing_message_sends_error(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """Missing message field should send error via Management API."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "test-conn-123"},
                json={
                    "type": "agent_chat",
                    "agent_id": "my-agent",
                    # message missing
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()
        call_args = mock_management_api.send_message.call_args
        assert call_args[0][0] == "test-conn-123"
        assert call_args[0][1]["type"] == "error"

    @pytest.mark.asyncio
    async def test_agent_chat_empty_fields_sends_error(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """Empty agent_id and message should send error via Management API."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "test-conn-123"},
                json={
                    "type": "agent_chat",
                    "agent_id": "",
                    "message": "",
                },
            )

        assert response.status_code == 200
        mock_management_api.send_message.assert_called_once()
        call_args = mock_management_api.send_message.call_args
        assert call_args[0][1]["type"] == "error"


class TestAgentChatBackgroundTask:
    """Tests for the _process_agent_chat_background function arguments."""

    @pytest.mark.asyncio
    async def test_background_task_receives_correct_args(
        self,
        test_app,
        mock_connection_service,
        mock_management_api,
        connected_user,
        mock_check_budget_allowed,
    ):
        """Background task should receive connection_id, user_id, agent_id, message."""
        with patch("routers.websocket_chat._process_agent_chat_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={
                        "type": "agent_chat",
                        "agent_id": "luna",
                        "message": "Tell me a story",
                    },
                )

        mock_bg.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            owner_id="test-user-456",
            agent_id="luna",
            message="Tell me a story",
        )


class TestProcessAgentChatBackground:
    """Tests for the _process_agent_chat_background function (RPC-based flow)."""

    @pytest.fixture
    def mock_ecs_manager(self):
        with patch("routers.websocket_chat.get_ecs_manager") as mock_getter:
            manager = AsyncMock()
            mock_getter.return_value = manager
            yield manager

    @pytest.fixture
    def mock_pool(self):
        with patch("routers.websocket_chat.get_gateway_pool") as mock_getter:
            pool = AsyncMock()
            pool.send_rpc = AsyncMock(return_value={"runId": "run-123", "status": "started"})
            # Plan 3 Task 4: chat.send path now calls gate_chat() before
            # forwarding to OpenClaw. Default to "not blocked" so existing
            # tests don't need to know about the gate.
            pool.gate_chat = AsyncMock(return_value={"blocked": False})
            mock_getter.return_value = pool
            yield pool

    @pytest.fixture
    def mock_mgmt_api(self):
        with patch("routers.websocket_chat.get_management_api_client") as mock_getter:
            client = MagicMock()
            client.send_message = MagicMock(return_value=True)
            mock_getter.return_value = client
            yield client

    @pytest.fixture
    def container_with_ip(self, mock_ecs_manager):
        """Set up ECS manager to return a running container with IP."""
        container = {
            "gateway_token": "test-gw-token",
            "owner_id": "user-1",
            "service_name": "openclaw-test",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        return container

    @pytest.mark.asyncio
    async def test_sends_chat_rpc(self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip):
        """Should send chat.send RPC via the gateway pool."""
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        mock_pool.send_rpc.assert_called_once()
        call_kwargs = mock_pool.send_rpc.call_args[1]
        assert call_kwargs["user_id"] == "user-1"
        assert call_kwargs["method"] == "chat.send"
        params = call_kwargs["params"]
        assert params["sessionKey"] == "agent:luna:user-1"
        assert params["message"] == "Hello!"
        assert "idempotencyKey" in params  # UUID, just check it's present
        assert call_kwargs["ip"] == "10.0.1.5"
        assert call_kwargs["token"] == "test-gw-token"

    @pytest.mark.asyncio
    async def test_no_container_sends_error(self, mock_ecs_manager, mock_pool, mock_mgmt_api):
        """No container should send error to frontend, not call pool."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        mock_pool.send_rpc.assert_not_called()
        mock_mgmt_api.send_message.assert_called_once()
        sent_msg = mock_mgmt_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "error"
        assert "No container" in sent_msg["message"]

    @pytest.mark.asyncio
    async def test_no_ip_sends_error(self, mock_ecs_manager, mock_pool, mock_mgmt_api):
        """Container without IP should send starting-up error."""
        container = MagicMock()
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, None))
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        mock_pool.send_rpc.assert_not_called()
        sent_msg = mock_mgmt_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "error"
        assert "starting up" in sent_msg["message"]

    @pytest.mark.asyncio
    async def test_rpc_error_sends_error_to_frontend(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """RPC failure should send error message to frontend."""
        mock_pool.send_rpc = AsyncMock(side_effect=RuntimeError("Connection lost"))
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        mock_mgmt_api.send_message.assert_called_once()
        sent_msg = mock_mgmt_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "error"
        assert "Connection lost" in sent_msg["message"]
