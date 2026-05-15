# backend/tests/unit/core/test_connection_pool.py
"""Unit tests for GatewayConnectionPool and GatewayConnection."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


class TestGatewayConnection:
    @pytest.fixture
    def mock_management_api(self):
        client = MagicMock()
        client.send_message = MagicMock(return_value=True)
        return client

    @pytest.fixture
    def connection(self, mock_management_api):
        return GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="test-user",
            ip="10.0.0.1",
            token="test-token",
            management_api=mock_management_api,
        )

    @pytest.mark.asyncio
    async def test_send_rpc_formats_request(self, connection):
        """send_rpc should send a properly formatted OpenClaw req message."""
        connection._ws = AsyncMock()
        await connection.send_rpc("req-123", "health", {})
        connection._ws.send.assert_called_once()
        sent = json.loads(connection._ws.send.call_args[0][0])
        assert sent == {"type": "req", "id": "req-123", "method": "health", "params": {}}

    @pytest.mark.asyncio
    async def test_handle_res_resolves_future(self, connection):
        """_handle_message with type=res should resolve the matching Future."""
        future = asyncio.get_event_loop().create_future()
        connection._pending_rpcs["req-456"] = future
        connection._handle_message({"type": "res", "id": "req-456", "ok": True, "payload": {"uptime": 3600}})
        result = await asyncio.wait_for(future, timeout=1)
        assert result == {"uptime": 3600}

    @pytest.mark.asyncio
    async def test_handle_res_error_rejects_future(self, connection):
        """_handle_message with type=res ok=false should reject the Future."""
        future = asyncio.get_event_loop().create_future()
        connection._pending_rpcs["req-789"] = future
        connection._handle_message({"type": "res", "id": "req-789", "ok": False, "error": {"message": "not found"}})
        with pytest.raises(RuntimeError, match="not found"):
            await asyncio.wait_for(future, timeout=1)

    def test_handle_event_forwards_to_frontend(self, connection, mock_management_api):
        """_handle_message with type=event should forward to all frontend connections."""
        connection._frontend_connections.add("conn-abc")
        connection._frontend_connections.add("conn-def")
        # Use sessions.updated — health/tick events are now intentionally dropped.
        connection._handle_message({"type": "event", "event": "sessions.updated", "payload": {"status": "ok"}})
        assert mock_management_api.send_message.call_count == 2

    def test_shared_frontend_connections(self, connection):
        """Frontend connections are managed via the shared set, not the connection."""
        # The pool owns the set; GatewayConnection reads it directly.
        connection._frontend_connections.add("conn-1")
        connection._frontend_connections.add("conn-2")
        assert len(connection._frontend_connections) == 2
        connection._frontend_connections.discard("conn-1")
        assert len(connection._frontend_connections) == 1

    @pytest.mark.asyncio
    async def test_close_cancels_reader(self, connection):
        """close() should cancel the reader task and close the WS."""
        connection._ws = AsyncMock()
        connection._reader_task = asyncio.create_task(asyncio.sleep(100))
        await connection.close()
        assert connection._reader_task.cancelled()
        connection._ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_for_response_times_out(self, connection):
        """wait_for_response should raise TimeoutError after timeout."""
        with pytest.raises(asyncio.TimeoutError):
            await connection.wait_for_response("req-timeout", timeout=0.1)
        assert "req-timeout" not in connection._pending_rpcs


class TestGatewayConnectionPool:
    @pytest.fixture
    def pool(self):
        return GatewayConnectionPool(management_api=MagicMock())

    def test_add_remove_frontend_connection(self, pool):
        """Should track user's frontend connections."""
        pool.add_frontend_connection("user-1", "conn-abc")
        assert "conn-abc" in pool._frontend_connections["user-1"]
        pool.remove_frontend_connection("user-1", "conn-abc")
        assert len(pool._frontend_connections.get("user-1", set())) == 0

    @pytest.mark.asyncio
    async def test_send_rpc_creates_connection(self, pool):
        """send_rpc should create a GatewayConnection if none exists."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.is_connected = True
        mock_conn.wait_for_response = AsyncMock(return_value={"status": "ok"})
        mock_conn._frontend_connections = set()
        with patch.object(pool, "_create_connection", return_value=mock_conn):
            result = await pool.send_rpc("user-1", "req-1", "health", {}, "10.0.0.1", "tok")
            assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_send_rpc_reuses_existing(self, pool):
        """send_rpc should reuse an existing connected GatewayConnection."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.is_connected = True
        mock_conn._parked = False  # send_rpc consults this before is_connected
        mock_conn.wait_for_response = AsyncMock(return_value={"data": 1})
        mock_conn._frontend_connections = set()
        pool._connections["user-1"] = mock_conn
        result = await pool.send_rpc("user-1", "req-2", "agents.list", {}, "10.0.0.1", "tok")
        assert result == {"data": 1}
        mock_conn.send_rpc.assert_called_once_with("req-2", "agents.list", {})

    @pytest.mark.asyncio
    async def test_close_all(self, pool):
        """close_all should close every connection."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        pool._connections["user-1"] = mock_conn
        await pool.close_all()
        mock_conn.close.assert_called_once()
        assert len(pool._connections) == 0

    @pytest.mark.asyncio
    async def test_send_rpc_replaces_disconnected_connection(self, pool):
        """send_rpc closes and replaces a connection that is no longer connected."""
        stale_conn = AsyncMock(spec=GatewayConnection)
        stale_conn.is_connected = False
        stale_conn._parked = False  # stale, not parked → close+recreate path
        pool._connections["user-1"] = stale_conn

        new_conn = AsyncMock(spec=GatewayConnection)
        new_conn.is_connected = True
        new_conn._parked = False
        new_conn.wait_for_response = AsyncMock(return_value={"fresh": True})
        new_conn._frontend_connections = set()

        with patch.object(pool, "_create_connection", return_value=new_conn):
            result = await pool.send_rpc("user-1", "req-x", "health", {}, "10.0.0.1", "tok")

        stale_conn.close.assert_called_once()
        assert result == {"fresh": True}

    @pytest.mark.asyncio
    async def test_grace_close_fires_after_last_frontend_disconnects(self, pool):
        """Removing the last frontend connection starts a grace period task."""
        pool.add_frontend_connection("user-gc", "conn-1")
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn._frontend_connections = set()
        pool._connections["user-gc"] = mock_conn

        with patch.object(pool, "_grace_close", AsyncMock()) as _mock_grace:
            pool.remove_frontend_connection("user-gc", "conn-1")
            # Grace task should have been created
            assert "user-gc" in pool._grace_tasks

    @pytest.mark.asyncio
    async def test_grace_close_cancelled_when_frontend_reconnects(self, pool):
        """Adding a frontend connection cancels the pending grace task."""
        pool.add_frontend_connection("user-gc2", "conn-1")
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn._frontend_connections = set()
        pool._connections["user-gc2"] = mock_conn

        # Start a real asyncio grace task (we'll cancel it)
        grace_task = asyncio.create_task(asyncio.sleep(100))
        pool._grace_tasks["user-gc2"] = grace_task

        # Adding a frontend connection cancels the grace task
        pool.add_frontend_connection("user-gc2", "conn-new")
        assert grace_task.cancelled() or "user-gc2" not in pool._grace_tasks


class TestGatewayConnectionHandshake:
    """Test _handshake and _verify_health edge cases.

    The handshake in OpenClaw 4.5 requires a signed Ed25519 device identity
    in the connect request (see `apps/backend/core/crypto/operator_device.py`
    and the OpenClaw reference `src/gateway/device-auth.ts`). Tests stub:

    - `container_repo.get_by_owner_id` → returns a row with an
      `operator_priv_key_enc` ciphertext (opaque blob — KMS is mocked).
    - `kms_secrets.decrypt_bytes` → returns a real 32-byte Ed25519 seed,
      so `sign_connect_request` can produce a real signature the test can
      assert on.
    """

    @pytest.fixture
    def connection(self):
        return GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="test-user",
            ip="10.0.0.1",
            token="test-token",
            management_api=MagicMock(),
        )

    @pytest.fixture
    def operator_seed(self):
        """Fresh 32-byte Ed25519 seed for the test run."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        priv = Ed25519PrivateKey.generate()
        return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    @pytest.fixture
    def mock_device_auth_deps(self, operator_seed):
        """Patch container_repo + kms_secrets so _handshake can load a real identity."""
        with (
            patch("core.repositories.container_repo.get_by_owner_id", new_callable=AsyncMock) as mock_get,
            patch("core.crypto.kms_secrets.decrypt_bytes") as mock_decrypt,
        ):
            mock_get.return_value = {
                "owner_id": "test-user",
                "operator_priv_key_enc": "opaque-kms-ciphertext-blob",
                "operator_device_id": "stub-device-id",
                "gateway_token": "test-token",
            }
            mock_decrypt.return_value = operator_seed
            yield mock_get, mock_decrypt

    @pytest.mark.asyncio
    async def test_handshake_fails_wrong_event(self, connection):
        """_handshake raises RuntimeError if first message is not connect.challenge."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"event": "something.else"}))
        connection._ws = mock_ws

        with pytest.raises(RuntimeError, match="Expected connect.challenge"):
            await connection._handshake()

    @pytest.mark.asyncio
    async def test_handshake_fails_connect_rejected(self, connection, mock_device_auth_deps):
        """_handshake raises RuntimeError if gateway rejects the connect request."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "test-nonce-123"}}),
                json.dumps({"ok": False, "error": {"message": "invalid token"}}),
            ]
        )
        connection._ws = mock_ws

        with pytest.raises(RuntimeError, match="Gateway connect failed: invalid token"):
            await connection._handshake()

    @pytest.mark.asyncio
    async def test_handshake_sends_signed_device(self, connection, mock_device_auth_deps):
        """_handshake sends a v3-scoped-auth connect request with a signed Ed25519 device.

        Verifies: role + protocol version + scopes list match what
        `BACKEND_OPERATOR_SCOPES` declares, the device fields are all
        present, and the signature verifies against the public key for the
        exact v2 payload the server will re-build.
        """
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "test-nonce-abc"}}),
                json.dumps({"ok": True}),
            ]
        )
        connection._ws = mock_ws

        await connection._handshake()

        sent = json.loads(mock_ws.send.call_args[0][0])
        params = sent["params"]

        # Connect request envelope
        assert sent["method"] == "connect"
        assert params["role"] == "operator"
        assert params["minProtocol"] == 4
        assert params["maxProtocol"] == 4
        # Backend needs admin for skills.install, agents.create/update/delete,
        # etc. — read/write alone was too restrictive. Admin short-circuits
        # per-method checks on the server (method-scopes.ts:232).
        assert params["scopes"] == ["operator.admin", "operator.read", "operator.write"]
        assert params["auth"]["token"] == "test-token"
        assert params["client"]["id"] == "gateway-client"
        assert params["client"]["mode"] == "backend"

        # Device block
        device = params["device"]
        assert set(device.keys()) == {"id", "publicKey", "signature", "signedAt", "nonce"}
        assert device["nonce"] == "test-nonce-abc"
        assert isinstance(device["signedAt"], int)
        assert device["signature"]  # non-empty

        # Signature must verify against the v2 payload format exactly as
        # the server will re-build it — mismatch here would mean the
        # client/server would disagree on scope binding and the server
        # would clear scopes, reproducing the original regression.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from core.crypto.operator_device import build_v2_connect_payload, _b64url_nopad_decode

        expected_payload = build_v2_connect_payload(
            device_id=device["id"],
            client_id="gateway-client",
            client_mode="backend",
            role="operator",
            scopes=["operator.admin", "operator.read", "operator.write"],
            signed_at_ms=device["signedAt"],
            token="test-token",
            nonce="test-nonce-abc",
        )
        pub = Ed25519PublicKey.from_public_bytes(_b64url_nopad_decode(device["publicKey"]))
        # Raises InvalidSignature if the payload or signature don't match.
        pub.verify(_b64url_nopad_decode(device["signature"]), expected_payload.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_verify_health_raises_on_unhealthy(self, connection):
        """_verify_health raises RuntimeError if gateway health check returns ok=false."""
        import uuid as uuid_mod

        req_id = str(uuid_mod.uuid4())
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "res", "id": req_id, "ok": False, "error": {"message": "not ready"}})
        )
        connection._ws = mock_ws

        # Patch uuid to make the req_id predictable
        with patch("core.gateway.connection_pool.uuid.uuid4", return_value=uuid_mod.UUID(req_id)):
            with pytest.raises(RuntimeError, match="Gateway not healthy"):
                await connection._verify_health()

    @pytest.mark.asyncio
    async def test_verify_health_skips_events_before_response(self, connection):
        """_verify_health discards interleaved event messages until the health response arrives."""
        import uuid as uuid_mod

        req_id = str(uuid_mod.uuid4())
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "event", "event": "startup"}),  # skip this
                json.dumps({"type": "res", "id": "other-req", "ok": True}),  # skip: wrong id
                json.dumps({"type": "res", "id": req_id, "ok": True}),  # correct response
            ]
        )
        connection._ws = mock_ws

        with patch("core.gateway.connection_pool.uuid.uuid4", return_value=uuid_mod.UUID(req_id)):
            # Should complete without error
            await connection._verify_health()


class TestTransformAgentEvent:
    """Test static helper for extracting streaming text from agent events."""

    def test_returns_chunk_for_assistant_stream(self):
        payload = {"stream": "assistant", "data": {"text": "Hello world"}, "runId": "run-1"}
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {"type": "chunk", "content": "Hello world", "runId": "run-1"}

    def test_returns_none_for_non_assistant_stream(self):
        payload = {"stream": "system", "data": {"text": "ignored"}}
        assert GatewayConnection._transform_agent_event(payload) is None

    def test_returns_none_when_no_stream_field(self):
        payload = {"data": {"text": "no stream key"}}
        assert GatewayConnection._transform_agent_event(payload) is None

    def test_returns_none_when_text_is_empty(self):
        payload = {"stream": "assistant", "data": {"text": ""}}
        assert GatewayConnection._transform_agent_event(payload) is None

    def test_returns_none_when_data_not_dict(self):
        payload = {"stream": "assistant", "data": "not-a-dict"}
        assert GatewayConnection._transform_agent_event(payload) is None

    def test_returns_none_when_no_data(self):
        payload = {"stream": "assistant"}
        assert GatewayConnection._transform_agent_event(payload) is None

    def test_forwards_run_id_on_chunk(self):
        payload = {
            "stream": "assistant",
            "runId": "run-abc-123",
            "data": {"text": "Hello"},
        }
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {
            "type": "chunk",
            "content": "Hello",
            "runId": "run-abc-123",
        }

    def test_forwards_run_id_on_thinking(self):
        payload = {
            "stream": "thinking",
            "runId": "run-xyz",
            "data": {"text": "let me think"},
        }
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {
            "type": "thinking",
            "content": "let me think",
            "runId": "run-xyz",
        }

    def test_forwards_run_id_on_tool_start(self):
        payload = {
            "stream": "tool",
            "runId": "run-tool-1",
            "data": {"phase": "start", "name": "exec", "toolCallId": "tc-1"},
        }
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {
            "type": "tool_start",
            "tool": "exec",
            "toolCallId": "tc-1",
            "runId": "run-tool-1",
        }

    def test_forwards_run_id_on_tool_end(self):
        payload = {
            "stream": "tool",
            "runId": "run-tool-2",
            "data": {"phase": "result", "name": "exec", "toolCallId": "tc-2"},
        }
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {
            "type": "tool_end",
            "tool": "exec",
            "toolCallId": "tc-2",
            "runId": "run-tool-2",
        }

    def test_forwards_run_id_on_tool_error(self):
        payload = {
            "stream": "tool",
            "runId": "run-tool-err",
            "data": {
                "phase": "result",
                "name": "exec",
                "toolCallId": "tc-err",
                "isError": True,
            },
        }
        result = GatewayConnection._transform_agent_event(payload)
        assert result == {
            "type": "tool_error",
            "tool": "exec",
            "toolCallId": "tc-err",
            "runId": "run-tool-err",
        }

    def test_raises_when_run_id_missing(self):
        """runId is a required field on agent events — absence is a protocol bug.

        Guarding would hide a contract violation (frontend keys per-run
        assistant bubbles on this id). Surface it loudly instead.
        """
        payload = {"stream": "assistant", "data": {"text": "Hello"}}
        with pytest.raises(KeyError):
            GatewayConnection._transform_agent_event(payload)


class TestHandleMessageChatEvents:
    """Test _handle_message routing for chat event states."""

    @pytest.fixture
    def connection(self):
        mgmt = MagicMock()
        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="test-user",
            ip="10.0.0.1",
            token="tok",
            management_api=mgmt,
        )
        conn._frontend_connections.add("frontend-1")
        return conn, mgmt

    def test_chat_final_sends_done(self, connection):
        """chat state=final forwards a done message to frontends."""
        conn, mgmt = connection
        conn._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "final", "runId": "run-1", "message": {"content": []}},
            }
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert {"type": "done", "runId": "run-1"} in calls

    def test_chat_final_emits_text_chunk_then_done(self, connection):
        """chat state=final forwards the assistant text as a chunk before the
        done signal. For agent runs the streamed chunks already painted the
        same text (cumulative replace ⇒ visual no-op). For slash commands
        (/help, /approve, etc.) this is the only path that delivers the
        response text to the frontend — without it, slash-command replies
        are silently dropped."""
        conn, mgmt = connection
        conn._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "message": {"content": [{"type": "text", "text": "complete answer"}]},
                },
            }
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert {"type": "chunk", "content": "complete answer", "runId": "run-1"} in calls
        assert {"type": "done", "runId": "run-1"} in calls
        # chunk before done
        assert calls.index({"type": "chunk", "content": "complete answer", "runId": "run-1"}) < calls.index(
            {"type": "done", "runId": "run-1"}
        )

    def test_chat_error_sends_error_message(self, connection):
        """chat state=error forwards an error type message."""
        conn, mgmt = connection
        conn._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-e", "error": {"message": "timeout"}},
            }
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert any(c.get("type") == "error" for c in calls)
        assert any("timeout" in c.get("message", "") for c in calls)

    def test_chat_aborted_sends_error_message(self, connection):
        """chat state=aborted forwards a cancellation error."""
        conn, mgmt = connection
        conn._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "aborted", "runId": "run-a"},
            }
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert any(c.get("type") == "error" for c in calls)

    def test_chat_delta_is_ignored(self, connection):
        """chat state=delta is silently ignored (agent events handle streaming)."""
        conn, mgmt = connection
        conn._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "delta"},
            }
        )
        mgmt.send_message.assert_not_called()

    def test_other_events_forwarded_as_is(self, connection):
        """Non-chat, non-agent events are forwarded unchanged for SWR revalidation."""
        conn, mgmt = connection
        msg = {"type": "event", "event": "sessions.updated", "payload": {}}
        conn._handle_message(msg)
        mgmt.send_message.assert_called_once_with("frontend-1", msg)


class _CrashingWs:
    """Minimal WebSocket mock that raises RuntimeError on first iteration."""

    def __init__(self, error="unexpected disconnect"):
        self._error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RuntimeError(self._error)

    async def close(self):
        pass


class TestReaderLoopCrash:
    """Test reader loop crash recovery."""

    @pytest.mark.asyncio
    async def test_reader_crash_rejects_pending_rpcs(self):
        """When the reader loop crashes, all pending RPCs are rejected."""
        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="test-user",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        conn._ws = _CrashingWs("unexpected disconnect")

        # Add a pending RPC
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        conn._pending_rpcs["req-pending"] = future

        # Run reader loop — should catch the error and reject the future
        await conn._reader_loop()

        assert future.done()
        with pytest.raises(RuntimeError, match="Gateway connection lost"):
            future.result()
        assert len(conn._pending_rpcs) == 0

    @pytest.mark.asyncio
    async def test_reader_does_not_reject_rpcs_when_closed(self):
        """If the connection was explicitly closed, reader errors are silently ignored."""
        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="test-user",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        conn._closed = True
        conn._ws = _CrashingWs("closed")

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        conn._pending_rpcs["req-closed"] = future

        await conn._reader_loop()

        # Future should not have been resolved/rejected by the reader
        assert not future.done()


class TestStatusChangeEvents:
    """Test that gateway connect/disconnect emits status_change events."""

    @pytest.mark.asyncio
    async def test_emits_connected_event_on_gateway_connect(self):
        """Should push status_change with state=HEALTHY when gateway connects."""
        mock_mgmt = MagicMock()
        mock_mgmt.send_message = MagicMock(return_value=True)

        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc"}

        conn._emit_status_change("HEALTHY", "Gateway connected")

        mock_mgmt.send_message.assert_called_once()
        call_args = mock_mgmt.send_message.call_args
        assert call_args[0][0] == "conn_abc"
        msg = json.loads(call_args[0][1]) if isinstance(call_args[0][1], str) else call_args[0][1]
        assert msg["type"] == "event"
        assert msg["event"] == "status_change"
        assert msg["payload"]["state"] == "HEALTHY"
        assert msg["payload"]["reason"] == "Gateway connected"

    @pytest.mark.asyncio
    async def test_emits_down_event_on_gateway_disconnect(self):
        """Should push status_change with state=GATEWAY_DOWN when gateway disconnects."""
        mock_mgmt = MagicMock()
        mock_mgmt.send_message = MagicMock(return_value=True)

        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc", "conn_def"}

        conn._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection lost")

        assert mock_mgmt.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_emit_status_change_handles_send_failure(self):
        """Should not raise if send_message fails for a connection."""
        mock_mgmt = MagicMock()
        mock_mgmt.send_message = MagicMock(side_effect=Exception("gone"))

        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc"}

        # Should not raise
        conn._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection lost")

    @pytest.mark.asyncio
    async def test_emit_status_change_no_frontends(self):
        """Should be a no-op when no frontend connections exist."""
        mock_mgmt = MagicMock()

        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )

        conn._emit_status_change("HEALTHY", "Gateway connected")

        mock_mgmt.send_message.assert_not_called()


class TestChatEventForwarding:
    """_handle_message forwards runId on chat terminal events (done/error)."""

    @pytest.fixture
    def mock_management_api(self):
        client = MagicMock()
        client.send_message = MagicMock(return_value=True)
        return client

    @pytest.fixture
    def connection(self, mock_management_api):
        c = GatewayConnection(
            frontend_connections={"conn-1"},
            conn_member_map={"conn-1": "user-1"},
            user_id="user-1",
            ip="10.0.0.1",
            token="t",
            management_api=mock_management_api,
        )
        return c

    def test_done_event_carries_run_id(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-done-1",
                },
            }
        )
        done_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "done"
        ]
        assert len(done_msgs) == 1
        assert done_msgs[0]["runId"] == "run-done-1"

    def test_error_event_carries_run_id(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-err-1",
                    "error": {"message": "model unavailable"},
                },
            }
        )
        error_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "error"
        ]
        assert len(error_msgs) == 1
        assert error_msgs[0]["runId"] == "run-err-1"
        assert error_msgs[0]["message"] == "model unavailable"

    def test_aborted_event_carries_run_id(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-abort-1",
                },
            }
        )
        error_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "error"
        ]
        assert len(error_msgs) == 1
        assert error_msgs[0]["runId"] == "run-abort-1"

    def test_done_without_run_id_raises(self, connection, mock_management_api):
        """runId is required on chat-terminal events. Missing runId is a
        protocol violation — fail loudly rather than silently forwarding
        without it (frontend multi-bubble routing would break)."""
        with pytest.raises(KeyError):
            connection._handle_message(
                {
                    "type": "event",
                    "event": "chat",
                    "payload": {
                        "state": "final",
                        "sessionKey": "agent:main:user-1",
                    },
                }
            )


class TestActiveRunTracking:
    """Active-run lifecycle: chat.send opens the window, chat-terminal
    events close it. Drives the parked-vs-dead decision when the reader
    loop exits, which in turn drives whether the cold-start
    ProvisioningStepper re-engages on top of a live chat. See
    `connection_pool._on_reader_loop_exit` for the dependent code.
    """

    @pytest.fixture
    def connection(self):
        return GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user-active-run",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )

    def test_mark_run_active_records_session(self, connection):
        connection.mark_run_active("agent:main:user-active-run", agent_id="main")
        assert "agent:main:user-active-run" in connection._active_runs
        run = connection._active_runs["agent:main:user-active-run"]
        assert run["agent_id"] == "main"
        assert run["started_at"] > 0
        assert run["last_event_at"] == run["started_at"]
        assert connection.has_active_runs is True

    def test_chat_final_clears_active_run(self, connection):
        """chat.final on the matching sessionKey clears the active run."""
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")
        connection._frontend_connections.add("conn-1")

        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            }
        )

        assert connection.has_active_runs is False
        assert session_key not in connection._active_runs

    def test_chat_error_clears_active_run(self, connection):
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")
        connection._frontend_connections.add("conn-1")

        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "sessionKey": session_key,
                    "runId": "run-err",
                    "errorMessage": "rate limited",
                    "errorKind": "rate_limit",
                },
            }
        )

        assert connection.has_active_runs is False

    def test_chat_aborted_clears_active_run(self, connection):
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")
        connection._frontend_connections.add("conn-1")

        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": session_key,
                    "runId": "run-abort",
                },
            }
        )

        assert connection.has_active_runs is False

    def test_touch_run_updates_last_event_at(self, connection):
        """Any inbound event tagged with the sessionKey bumps last_event_at —
        signal the deadlock watchdog uses to distinguish a stuck container
        from a slow turn."""
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")
        original = connection._active_runs[session_key]["last_event_at"]

        # Wait a beat so the monotonic clock advances.
        import time as _t

        _t.sleep(0.01)

        # Agent stream event for the same session
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {"text": "tok"},
                },
            }
        )

        bumped = connection._active_runs[session_key]["last_event_at"]
        assert bumped > original

    def test_touch_run_noop_for_unknown_session(self, connection):
        """Untracked sessionKeys (cron / channel) must not create entries."""
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "sessionKey": "agent:cron-agent:cron:job-7:run:r-1",
                    "runId": "run-cron",
                    "data": {"text": "tok"},
                },
            }
        )
        assert connection.has_active_runs is False


class TestReaderLoopExitBranches:
    """`_on_reader_loop_exit` decides between PARKED (active runs in flight)
    and DEAD (no active runs). PARKED preserves `_pending_rpcs` and the
    `is_alive` property so the cold-start phase resolver does NOT regress
    /container/status back to "starting".
    """

    @pytest.fixture
    def make_connection(self):
        def _make():
            return GatewayConnection(
                frontend_connections=set(),
                conn_member_map={},
                user_id="user-exit-branch",
                ip="10.0.0.1",
                token="tok",
                management_api=MagicMock(return_value=True),
            )

        return _make

    @pytest.mark.asyncio
    async def test_exit_parks_when_active_runs_in_flight(self, make_connection):
        """Reader-loop exception with non-empty active_runs → park, do NOT
        reject pending RPCs, do NOT clear active_runs, emit
        GATEWAY_DOWN_LLM_BUSY (not GATEWAY_DOWN_DEAD)."""
        conn = make_connection()
        conn._frontend_connections.add("conn-1")
        conn.mark_run_active("agent:main:user-exit-branch", agent_id="main")

        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        conn._pending_rpcs["sessions-list-1"] = pending

        try:
            conn._on_reader_loop_exit(RuntimeError("ping timeout"))

            # _pending_rpcs preserved — the chat.send ack already returned,
            # but a concurrent sessions.list still wants to resolve when the
            # reopen lands. Rejecting here would manufacture a phantom error.
            assert pending.done() is False
            assert "sessions-list-1" in conn._pending_rpcs

            # Active runs preserved — they're proof of life.
            assert conn.has_active_runs is True

            # Marked parked.
            assert conn._parked is True
            # is_alive remains True so the cold-start phase resolver keeps
            # reporting "ready" and ProvisioningStepper stays suppressed.
            assert conn.is_alive is True
            assert conn.is_connected is False  # WS itself is gone

            # Last status_change event was the LLM_BUSY variant.
            sent_msgs = [c.args[1] for c in conn._management_api.send_message.call_args_list]
            status_events = [m for m in sent_msgs if isinstance(m, dict) and m.get("event") == "status_change"]
            assert status_events, "expected at least one status_change emit"
            assert status_events[-1]["payload"]["state"] == "GATEWAY_DOWN_LLM_BUSY"

            # Watchdog spawned with bounded lifetime.
            assert conn._parked_watchdog_task is not None
            assert not conn._parked_watchdog_task.done()
        finally:
            if conn._parked_watchdog_task and not conn._parked_watchdog_task.done():
                conn._parked_watchdog_task.cancel()
                try:
                    await conn._parked_watchdog_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_exit_dies_when_no_active_runs(self, make_connection):
        """Reader-loop exception with empty active_runs → DEAD: reject
        pending RPCs (today's behavior intact), emit GATEWAY_DOWN_DEAD,
        do not park, do not spawn watchdog."""
        conn = make_connection()
        conn._frontend_connections.add("conn-1")

        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        conn._pending_rpcs["agents-list-1"] = pending

        conn._on_reader_loop_exit(RuntimeError("connection refused"))

        assert pending.done()
        with pytest.raises(RuntimeError, match="Gateway connection lost"):
            pending.result()
        assert len(conn._pending_rpcs) == 0

        assert conn._parked is False
        assert conn.is_alive is False
        assert conn._parked_watchdog_task is None

        sent_msgs = [c.args[1] for c in conn._management_api.send_message.call_args_list]
        status_events = [m for m in sent_msgs if isinstance(m, dict) and m.get("event") == "status_change"]
        assert status_events, "expected at least one status_change emit"
        assert status_events[-1]["payload"]["state"] == "GATEWAY_DOWN_DEAD"

    @pytest.mark.asyncio
    async def test_is_alive_falls_to_false_when_parked_and_no_runs(self, make_connection):
        """A parked conn without active runs is no longer alive — covers the
        watchdog escalation path (clears _active_runs then checks).
        """
        conn = make_connection()
        conn._parked = True
        # No active runs.
        assert conn.is_alive is False


class TestPoolReconnectFloor:
    """The pool's _respect_reconnect_floor blocks repeat reopen / recreate
    within `_RECONNECT_FLOOR_S` of the last attempt. Protects the backend
    open-handshake budget when a wedged container collides with frontend
    exponential retry."""

    @pytest.mark.asyncio
    async def test_first_reconnect_attempt_allowed(self):
        pool = GatewayConnectionPool(management_api=MagicMock())
        # No prior attempt — should not raise.
        pool._respect_reconnect_floor("user-1")
        assert "user-1" in pool._last_reconnect_at

    @pytest.mark.asyncio
    async def test_immediate_second_attempt_throttled(self):
        pool = GatewayConnectionPool(management_api=MagicMock())
        pool._respect_reconnect_floor("user-1")
        with pytest.raises(RuntimeError, match="throttled"):
            pool._respect_reconnect_floor("user-1")

    @pytest.mark.asyncio
    async def test_pool_mark_run_active_noop_when_no_connection(self):
        """The chat dispatcher calls mark_run_active BEFORE send_rpc creates
        the connection on a cold start. The call must be safe to issue when
        no connection exists — the first inbound event keyed on the same
        sessionKey will populate _active_runs once the conn is up."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        # Should not raise.
        pool.mark_run_active("user-1", "agent:main:user-1", "main")

    @pytest.mark.asyncio
    async def test_is_user_connected_true_for_parked_with_runs(self):
        """The cold-start phase resolver consults is_user_connected. A
        parked connection with active runs must read as connected so
        /container/status keeps reporting phase=ready and
        ProvisioningStepper does NOT re-engage."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id="user-parked",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        conn._parked = True
        conn.mark_run_active("agent:main:user-parked", "main")
        pool._connections["user-parked"] = conn

        assert pool.is_user_connected("user-parked") is True
