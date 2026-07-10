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

from routers.websocket_chat import (
    router,
    _process_agent_chat_background,
    _process_agent_abort_background,
    _process_agent_steer_background,
)


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
            # Frontend payload omitted clientRunId; backend forwards None so
            # _process_agent_chat_background mints a fallback UUID downstream.
            client_run_id=None,
            # Default (non-side) send: side=False unless the payload opts into
            # the /btw side-channel.
            side=False,
            # Payload omitted session_id -> forwarded as None (legacy "General"
            # session key). A UUID here would open a distinct conversation.
            session_id=None,
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
            # chat.send path calls gate_chat() before forwarding to
            # OpenClaw. Default to "not blocked" so existing tests don't
            # need to know about the gate.
            pool.gate_chat = AsyncMock(return_value={"blocked": False})
            # mark_run_active / clear_active_run / mark_side_run are SYNC on the
            # real pool; stub as MagicMock so calling them doesn't spawn
            # un-awaited coroutines (which surface as RuntimeWarnings).
            pool.mark_run_active = MagicMock()
            pool.clear_active_run = MagicMock()
            pool.mark_side_run = MagicMock()
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
        # deliver:false mirrors upstream web-UI requestChatSend — a direct
        # streaming client must not also re-deliver the reply to channels.
        assert params["deliver"] is False
        assert call_kwargs["ip"] == "10.0.1.5"
        assert call_kwargs["token"] == "test-gw-token"

    @pytest.mark.asyncio
    async def test_ensures_connection_before_marking_run_active(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """The parked-vs-dead window must open even on the FIRST send to a fresh
        task. mark_run_active no-ops without a connection (and nothing else
        populates _active_runs), and send_rpc is what creates the connection —
        so the dispatcher must ensure_connection BEFORE marking, otherwise the
        first run has no window and a WS drop would tear the connection down
        mid-run instead of parking it.
        """
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        mock_pool.ensure_connection.assert_awaited_once_with("user-1", "10.0.1.5", "test-gw-token")
        mock_pool.mark_run_active.assert_called_once_with("user-1", "agent:luna:user-1", "luna", member_id="user-1")

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

    @pytest.mark.asyncio
    async def test_chat_send_failure_clears_active_run(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """If chat.send fails AFTER mark_run_active opened the parked-vs-dead
        window, the window must be torn back down. Otherwise no terminal event
        will ever arrive to clear it and the stale entry keeps the connection
        parking on a WS drop indefinitely."""
        mock_pool.send_rpc = AsyncMock(side_effect=RuntimeError("Connection lost"))
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="Hello!",
        )
        # The window was opened then torn down for this exact session.
        mock_pool.mark_run_active.assert_called_once_with("user-1", "agent:luna:user-1", "luna", member_id="user-1")
        mock_pool.clear_active_run.assert_called_once_with("user-1", "agent:luna:user-1")
        # The user still gets the failure notice.
        sent_msg = mock_mgmt_api.send_message.call_args[0][1]
        assert sent_msg["type"] == "error"

    @pytest.mark.asyncio
    async def test_side_send_failure_does_not_clear_active_run(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """A side (/btw) send never opens the primary run's active-run window,
        so a send failure on it must NOT tear down that window — the primary
        run is still legitimately in flight."""
        mock_pool.send_rpc = AsyncMock(side_effect=RuntimeError("Connection lost"))
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="quick aside",
            side=True,
        )
        mock_pool.mark_run_active.assert_not_called()
        mock_pool.clear_active_run.assert_not_called()


# ---------------------------------------------------------------------------
# agent_abort  ->  chat.abort
# ---------------------------------------------------------------------------


class TestAgentAbortRouting:
    """ws_message routes agent_abort to the abort background task."""

    @pytest.mark.asyncio
    async def test_agent_abort_with_run_id_queued(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """agent_abort with a runId queues the abort task carrying that runId."""
        with patch("routers.websocket_chat._process_agent_abort_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={"type": "agent_abort", "agent_id": "luna", "runId": "run-9"},
                )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()
        mock_bg.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            owner_id="test-user-456",
            agent_id="luna",
            run_id="run-9",
            session_id=None,
        )

    @pytest.mark.asyncio
    async def test_agent_abort_without_run_id_queued(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """agent_abort without a runId still queues the abort task (run_id=None)."""
        with patch("routers.websocket_chat._process_agent_abort_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={"type": "agent_abort", "agent_id": "luna"},
                )

        assert response.status_code == 200
        mock_bg.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            owner_id="test-user-456",
            agent_id="luna",
            run_id=None,
            session_id=None,
        )

    @pytest.mark.asyncio
    async def test_agent_abort_missing_agent_id_sends_error(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """Missing agent_id should send an error and not queue the task."""
        with patch("routers.websocket_chat._process_agent_abort_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={"type": "agent_abort", "runId": "run-9"},
                )

        assert response.status_code == 200
        mock_bg.assert_not_called()
        mock_management_api.send_message.assert_called_once()
        assert mock_management_api.send_message.call_args[0][1]["type"] == "error"


class TestProcessAgentAbortBackground:
    """_process_agent_abort_background forwards chat.abort with the right params."""

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
            pool.send_rpc = AsyncMock(return_value={})
            # clear_active_run is SYNC on the real pool — stub as MagicMock so
            # calling it doesn't spawn an un-awaited coroutine.
            pool.clear_active_run = MagicMock()
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
        container = {"gateway_token": "test-gw-token", "owner_id": "user-1"}
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        return container

    @pytest.mark.asyncio
    async def test_abort_with_run_id_forwards_session_key_and_run_id(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """chat.abort carries {sessionKey, runId} when runId is present."""
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id="run-9",
        )
        mock_pool.send_rpc.assert_called_once()
        kw = mock_pool.send_rpc.call_args[1]
        assert kw["method"] == "chat.abort"
        assert kw["params"] == {"sessionKey": "agent:luna:user-1", "runId": "run-9"}
        assert kw["ip"] == "10.0.1.5"
        assert kw["token"] == "test-gw-token"

    @pytest.mark.asyncio
    async def test_abort_without_run_id_forwards_session_key_only(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """chat.abort carries {sessionKey} only when runId is absent."""
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id=None,
        )
        mock_pool.send_rpc.assert_called_once()
        kw = mock_pool.send_rpc.call_args[1]
        assert kw["method"] == "chat.abort"
        assert kw["params"] == {"sessionKey": "agent:luna:user-1"}

    @pytest.mark.asyncio
    async def test_abort_does_not_clear_active_run(self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip):
        """Abort must NOT clear the active-run window — the terminal
        aborted/run_ended event from the stream owns that transition."""
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id="run-9",
        )
        # The pool's run-clearing entry points must not be invoked here.
        mock_pool.clear_active_run.assert_not_called()
        mock_pool.mark_run_active.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_no_container_emits_synthetic_aborted_terminal(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api
    ):
        """When there's no live container/IP the abort can't be forwarded —
        but the frontend keeps isStreaming + the active runId until a SERVER
        terminal arrives. The abort path must synthesize the SAME `error`
        ("Agent run was cancelled") frame a real abort produces so the client
        tears down its live turn, clears isStreaming, and drains the queue."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id="run-9",
        )
        # No chat.abort RPC could be forwarded.
        mock_pool.send_rpc.assert_not_called()
        # A terminal error frame was pushed to unblock the client, tagged with
        # the aborted runId so the frontend's error handler drains the queue.
        mock_mgmt_api.send_message.assert_called_once()
        conn_arg, frame = mock_mgmt_api.send_message.call_args[0]
        assert conn_arg == "conn-1"
        assert frame["type"] == "error"
        assert frame["agent_id"] == "luna"
        assert frame["runId"] == "run-9"
        assert frame["message"] == "Agent run was cancelled"
        # And the stale parked-vs-dead window is cleared — with no container
        # left to emit a real terminal, nothing else would clear _active_runs,
        # so the connection would otherwise park "busy" forever.
        mock_pool.clear_active_run.assert_called_once_with("user-1", "agent:luna:user-1")

    @pytest.mark.asyncio
    async def test_abort_no_container_without_run_id_omits_run_id(self, mock_ecs_manager, mock_pool, mock_mgmt_api):
        """The synthetic terminal omits runId when the client didn't supply one
        (the frontend's error handler still clears isStreaming on a no-runId
        error — it just can't key the queue drain)."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id=None,
        )
        mock_mgmt_api.send_message.assert_called_once()
        _, frame = mock_mgmt_api.send_message.call_args[0]
        assert frame["type"] == "error"
        assert "runId" not in frame

    @pytest.mark.asyncio
    async def test_abort_rpc_failure_emits_synthetic_aborted_terminal(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """If the chat.abort RPC fails AFTER a container was resolved (RPC
        timeout, dead backend<->container WS), the client must still be
        unblocked — cancelMessage keeps isStreaming until a SERVER terminal, and
        if the WS to the container is gone the run's own terminal will never
        arrive. The catch synthesizes the same aborted terminal as the
        no-container branch."""
        mock_pool.send_rpc = AsyncMock(side_effect=ConnectionError("gateway gone"))
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id="run-9",
        )
        mock_mgmt_api.send_message.assert_called_once()
        conn_arg, frame = mock_mgmt_api.send_message.call_args[0]
        assert conn_arg == "conn-1"
        assert frame["type"] == "error"
        assert frame["agent_id"] == "luna"
        assert frame["runId"] == "run-9"
        assert frame["message"] == "Agent run was cancelled"

    @pytest.mark.asyncio
    async def test_abort_invalid_session_id_emits_friendly_terminal_not_raise(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """An invalid session_id makes build_chat_session_key raise. Because the
        key is now built INSIDE the try (mirroring the chat path), that raise
        surfaces the synthetic aborted terminal via the except instead of an
        unhandled BackgroundTasks exception. No abort RPC is forwarded — the key
        never validated."""
        # Must not raise despite the malformed trailing session id.
        await _process_agent_abort_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            run_id="run-9",
            session_id="not-a-uuid",
        )
        mock_pool.send_rpc.assert_not_called()
        mock_mgmt_api.send_message.assert_called_once()
        _, frame = mock_mgmt_api.send_message.call_args[0]
        assert frame["type"] == "error"
        assert frame["message"] == "Agent run was cancelled"


# ---------------------------------------------------------------------------
# agent_steer  ->  chat.send (deliver:false, fresh idempotencyKey)
# ---------------------------------------------------------------------------


class TestAgentSteerRouting:
    """ws_message routes agent_steer to the steer background task."""

    @pytest.mark.asyncio
    async def test_agent_steer_queued(self, test_app, mock_connection_service, mock_management_api, connected_user):
        with patch("routers.websocket_chat._process_agent_steer_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={
                        "type": "agent_steer",
                        "agent_id": "luna",
                        "text": "actually, focus on the API",
                    },
                )

        assert response.status_code == 200
        mock_management_api.send_message.assert_not_called()
        mock_bg.assert_called_once_with(
            connection_id="test-conn-123",
            user_id="test-user-456",
            owner_id="test-user-456",
            agent_id="luna",
            text="actually, focus on the API",
            session_id=None,
        )

    @pytest.mark.asyncio
    async def test_agent_steer_missing_text_sends_error(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        with patch("routers.websocket_chat._process_agent_steer_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={"type": "agent_steer", "agent_id": "luna"},
                )

        assert response.status_code == 200
        mock_bg.assert_not_called()
        mock_management_api.send_message.assert_called_once()
        assert mock_management_api.send_message.call_args[0][1]["type"] == "error"


class TestProcessAgentSteerBackground:
    """_process_agent_steer_background forwards chat.send with deliver:false."""

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
            pool.send_rpc = AsyncMock(return_value={"runId": "run-steer", "status": "started"})
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
        container = {"gateway_token": "test-gw-token", "owner_id": "user-1"}
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        return container

    @pytest.mark.asyncio
    async def test_steer_forwards_chat_send_deliver_false(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """Steer is upstream's chat.send with deliver:false + a fresh
        idempotencyKey on the SAME sessionKey — the container's queue-mode
        steer injects the text into the in-flight run."""
        await _process_agent_steer_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            text="focus on the API",
        )
        mock_pool.send_rpc.assert_called_once()
        kw = mock_pool.send_rpc.call_args[1]
        assert kw["method"] == "chat.send"
        params = kw["params"]
        assert params["sessionKey"] == "agent:luna:user-1"
        assert params["message"] == "focus on the API"
        assert params["deliver"] is False
        # Steer always mints a fresh run id (upstream generateUUID()); the
        # in-flight run is targeted by sessionKey, not by this key.
        assert isinstance(params["idempotencyKey"], str) and params["idempotencyKey"]
        assert kw["ip"] == "10.0.1.5"
        assert kw["token"] == "test-gw-token"

    @pytest.mark.asyncio
    async def test_steer_does_not_open_a_new_active_run(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """Steer injects into an EXISTING run; it must not open a new
        active-run window (that belongs to the original agent_chat send)."""
        await _process_agent_steer_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            text="focus on the API",
        )
        mock_pool.mark_run_active.assert_not_called()

    @pytest.mark.asyncio
    async def test_steer_invalid_session_id_emits_steer_failed_not_raise(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """An invalid session_id makes build_chat_session_key raise. Built inside
        the try (mirroring the chat path), it surfaces the steer_failed error
        frame (which the frontend treats as a non-terminal notice) instead of an
        unhandled exception. No chat.send is forwarded."""
        await _process_agent_steer_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            text="focus on the API",
            session_id="not-a-uuid",
        )
        mock_pool.send_rpc.assert_not_called()
        mock_mgmt_api.send_message.assert_called_once()
        _, frame = mock_mgmt_api.send_message.call_args[0]
        assert frame["type"] == "error"
        assert frame["code"] == "steer_failed"


# ---------------------------------------------------------------------------
# agent_chat side-channel (/btw)  ->  chat.send, runs alongside the primary
# ---------------------------------------------------------------------------


class TestAgentChatSideChannel:
    """A side:true agent_chat is a /btw side run: it forwards a normal
    chat.send (the container detects the /btw prefix) but must NOT claim the
    primary run's active-run window — it is tracked separately via
    mark_side_run so its terminal can't clear the primary's window."""

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
            pool.send_rpc = AsyncMock(return_value={"runId": "run-btw", "status": "started"})
            pool.gate_chat = AsyncMock(return_value={"blocked": False})
            # mark_run_active / clear_active_run / mark_side_run are SYNC on the
            # real pool; stub as MagicMock so calls don't spawn un-awaited
            # coroutines.
            pool.mark_run_active = MagicMock()
            pool.clear_active_run = MagicMock()
            pool.mark_side_run = MagicMock()
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
        container = {"gateway_token": "test-gw-token", "owner_id": "user-1"}
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        return container

    @pytest.mark.asyncio
    async def test_side_message_routed_with_side_flag(
        self, test_app, mock_connection_service, mock_management_api, connected_user
    ):
        """ws_message forwards side=True through to the background task."""
        with patch("routers.websocket_chat._process_agent_chat_background") as mock_bg:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                await client.post(
                    "/ws/message",
                    headers={"x-connection-id": "test-conn-123"},
                    json={
                        "type": "agent_chat",
                        "agent_id": "luna",
                        "message": "/btw what's the weather",
                        "side": True,
                    },
                )

        mock_bg.assert_called_once()
        assert mock_bg.call_args[1]["side"] is True

    @pytest.mark.asyncio
    async def test_side_message_does_not_mark_run_active(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """A side run forwards chat.send but does NOT take over the primary
        run's active-run window (it is tracked separately via mark_side_run)."""
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="/btw what's the weather",
            side=True,
        )
        mock_pool.send_rpc.assert_called_once()
        kw = mock_pool.send_rpc.call_args[1]
        assert kw["method"] == "chat.send"
        assert kw["params"]["message"] == "/btw what's the weather"
        # Side sends carry deliver:false on the same wire as a normal send —
        # the container detects the /btw prefix and runs it as a side run.
        assert kw["params"]["deliver"] is False
        # A side run ensures the connection (its events arrive there + the side
        # runId is registered so its terminal won't clear the primary window),
        # and registers as a side run — but must NOT claim the primary's
        # active-run window.
        mock_pool.ensure_connection.assert_awaited_once()
        mock_pool.mark_side_run.assert_called_once()
        mock_pool.mark_run_active.assert_not_called()
        # The side runId registered MUST equal the chat.send idempotencyKey, so
        # the runId OpenClaw stamps on the side run's events/terminal matches
        # what the terminal handler checks against the side-run set.
        assert mock_pool.mark_side_run.call_args[0][1] == kw["params"]["idempotencyKey"]

    @pytest.mark.asyncio
    async def test_non_side_message_marks_run_active(
        self, mock_ecs_manager, mock_pool, mock_mgmt_api, container_with_ip
    ):
        """A normal (non-side) chat.send DOES open the active-run window."""
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            message="hello",
        )
        mock_pool.mark_run_active.assert_called_once()
