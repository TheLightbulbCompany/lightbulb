# backend/tests/unit/core/test_connection_pool.py
"""Unit tests for GatewayConnectionPool and GatewayConnection."""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


async def _handle_and_flush(conn, msg):
    """Route *msg* then drain the FrameSender so send_message assertions see
    delivered frames (delivery runs on per-connection drainer tasks now)."""
    await conn._handle_message(msg)
    await conn._sender.flush()


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
        await _handle_and_flush(connection, {"type": "res", "id": "req-456", "ok": True, "payload": {"uptime": 3600}})
        result = await asyncio.wait_for(future, timeout=1)
        assert result == {"uptime": 3600}

    @pytest.mark.asyncio
    async def test_handle_res_error_rejects_future(self, connection):
        """_handle_message with type=res ok=false should reject the Future."""
        future = asyncio.get_event_loop().create_future()
        connection._pending_rpcs["req-789"] = future
        await _handle_and_flush(
            connection, {"type": "res", "id": "req-789", "ok": False, "error": {"message": "not found"}}
        )
        with pytest.raises(RuntimeError, match="not found"):
            await asyncio.wait_for(future, timeout=1)

    @pytest.mark.asyncio
    async def test_handle_event_forwards_to_frontend(self, connection, mock_management_api):
        """_handle_message with type=event fans out to the owner's chat connections
        sourced from the by-owner-id GSI."""
        connection._conn_svc.query_by_owner = AsyncMock(
            return_value=[("conn-abc", None, "chat"), ("conn-def", None, "chat")]
        )
        # Use sessions.updated — health/tick events are now intentionally dropped.
        await _handle_and_flush(connection, {"type": "event", "event": "sessions.updated", "payload": {"status": "ok"}})
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


class TestGatewayConnectionOwnerId:
    """GatewayConnection exposes the owner key it is pooled under.

    The pool's `_connections` dict is keyed by owner_id (org_id or user_id)
    and `_create_connection` passes that key in as `user_id`. The explicit
    `owner_id` attribute makes that key unambiguous for the owner-keyed
    DDB fan-out (it calls `query_by_owner(self.owner_id)`).
    """

    def test_owner_id_defaults_to_user_id(self):
        """Direct constructors (no owner_id) fall back to user_id."""
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user-personal",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        assert conn.owner_id == "user-personal"

    @pytest.mark.asyncio
    async def test_create_connection_sets_owner_id_to_pool_key(self):
        """The pool creates connections under an owner key (org_id or
        user_id) and that key is exposed as `owner_id`."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        created = {}

        original_init = GatewayConnection.__init__

        def _capturing_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created["conn"] = self

        # Skip the real WS connect — we only assert the constructed attribute.
        with patch.object(GatewayConnection, "connect", new=AsyncMock()):
            with patch.object(GatewayConnection, "__init__", new=_capturing_init):
                await pool._create_connection("org_456", "10.0.0.2", "tok")

        assert created["conn"].owner_id == "org_456"


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

    def test_add_frontend_connection_invalidates_recipient_cache(self, pool):
        """Registering a new tab must bust the owning connection's recipient
        cache so the new tab is picked up on its own first stream instead of
        waiting behind a stale cached set (Codex L693)."""
        conn = MagicMock(spec=GatewayConnection)
        pool._connections["org-1"] = conn
        pool.add_frontend_connection("org-1", "conn-new")
        conn.invalidate_recipients_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_frontend_connection_invalidates_recipient_cache(self, pool):
        """Removing a tab also busts the cache so it stops receiving promptly.
        (Async because the no-connections-left branch schedules _grace_close,
        which needs a running loop.)"""
        conn = MagicMock(spec=GatewayConnection)
        pool._connections["org-1"] = conn
        # Two tabs registered, so removing one does NOT trigger grace teardown.
        pool.add_frontend_connection("org-1", "conn-a")
        pool.add_frontend_connection("org-1", "conn-b")
        conn.invalidate_recipients_cache.reset_mock()
        pool.remove_frontend_connection("org-1", "conn-a")
        conn.invalidate_recipients_cache.assert_called_once()

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
    async def test_ensure_connection_creates_when_absent(self, pool):
        """ensure_connection get-or-creates the per-user connection and returns
        it, so the chat dispatcher can guarantee a connection exists BEFORE
        marking a run active — pool.mark_run_active no-ops without one, and
        nothing else populates _active_runs."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.is_connected = True
        mock_conn._frontend_connections = set()
        with patch.object(pool, "_create_connection", return_value=mock_conn) as create:
            conn = await pool.ensure_connection("user-1", "10.0.0.1", "tok")
            assert conn is mock_conn
            create.assert_awaited_once_with("user-1", "10.0.0.1", "tok")

    @pytest.mark.asyncio
    async def test_send_rpc_reuses_existing(self, pool):
        """send_rpc should reuse an existing connected GatewayConnection."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.ip = "10.0.0.1"  # matches the resolved ip → adoption is a no-op
        mock_conn.is_connected = True
        mock_conn._parked = False  # send_rpc consults this before is_connected
        mock_conn.wait_for_response = AsyncMock(return_value={"data": 1})
        mock_conn._frontend_connections = set()
        pool._connections["user-1"] = mock_conn
        result = await pool.send_rpc("user-1", "req-2", "agents.list", {}, "10.0.0.1", "tok")
        assert result == {"data": 1}
        # The pool mints a fresh, unique wire id per call — the client req_id is
        # namespaced (kept as a prefix for debuggability) and is never used as
        # the raw pending-RPC key, so two callers with the same client id can't
        # collide. (GW-3.)
        wire_id, sent_method, sent_params = mock_conn.send_rpc.call_args[0]
        assert wire_id.startswith("req-2-")
        assert (sent_method, sent_params) == ("agents.list", {})

    @pytest.mark.asyncio
    async def test_send_rpc_forwards_explicit_timeout(self, pool):
        """An explicit ``timeout`` on the pool's send_rpc must reach
        ``wait_for_response`` — the pairing RPCs rely on this to get real
        headroom (90s) instead of the default ``_RPC_TIMEOUT`` (30s)."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.ip = "10.0.0.1"
        mock_conn.is_connected = True
        mock_conn._parked = False
        mock_conn.wait_for_response = AsyncMock(return_value={"ok": True})
        mock_conn._frontend_connections = set()
        pool._connections["user-1"] = mock_conn

        result = await pool.send_rpc("user-1", "req-t", "agents.create", {}, "10.0.0.1", "tok", timeout=90)

        assert result == {"ok": True}
        # send + wait must share the SAME minted wire id, and the timeout must
        # still be forwarded on it. (GW-3.)
        wire_id = mock_conn.send_rpc.call_args[0][0]
        assert wire_id.startswith("req-t-")
        mock_conn.wait_for_response.assert_called_once_with(wire_id, timeout=90)

    @pytest.mark.asyncio
    async def test_send_rpc_omits_timeout_by_default(self, pool):
        """Without an explicit ``timeout``, send_rpc must call
        ``wait_for_response`` with no timeout kwarg at all, so that method's
        own ``_RPC_TIMEOUT`` default keeps applying to every existing caller."""
        mock_conn = AsyncMock(spec=GatewayConnection)
        mock_conn.ip = "10.0.0.1"
        mock_conn.is_connected = True
        mock_conn._parked = False
        mock_conn.wait_for_response = AsyncMock(return_value={"ok": True})
        mock_conn._frontend_connections = set()
        pool._connections["user-1"] = mock_conn

        result = await pool.send_rpc("user-1", "req-d", "agents.list", {}, "10.0.0.1", "tok")

        assert result == {"ok": True}
        # No timeout kwarg at all (preserves wait_for_response's own default),
        # but still on the minted wire id. (GW-3.)
        wire_id = mock_conn.send_rpc.call_args[0][0]
        assert wire_id.startswith("req-d-")
        mock_conn.wait_for_response.assert_called_once_with(wire_id)

    @pytest.mark.asyncio
    async def test_send_rpc_recreates_on_changed_ip_even_when_parked(self, pool):
        """A CHANGED task IP is proof of task replacement, so even a parked
        connection (which normally reopens to preserve run state) is torn down
        and recreated against the fresh endpoint — the parked run is on a dead
        task and cannot be preserved, and reopening to a new IP would resurrect
        stale run state against a fresh task that never had it. (GW-1.)"""
        parked = AsyncMock(spec=GatewayConnection)
        parked.ip = "10.0.0.1"  # stale: previous task
        parked._parked = True
        parked.is_connected = False
        parked._frontend_connections = set()
        pool._connections["user-1"] = parked

        new_conn = AsyncMock(spec=GatewayConnection)
        new_conn._frontend_connections = set()
        new_conn.wait_for_response = AsyncMock(return_value={"ok": True})

        with patch.object(pool, "_create_connection", return_value=new_conn) as create:
            result = await pool.send_rpc("user-1", "r", "agents.list", {}, "10.0.0.2", "tok")

        parked.reopen.assert_not_called()
        create.assert_awaited_once()
        assert result == {"ok": True}
        # Torn down in the BACKGROUND (off the lock) — drain the close task.
        await asyncio.gather(*pool._background_close_tasks)
        parked.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_rpc_reopens_parked_on_unchanged_ip(self, pool):
        """A parked connection on the SAME IP (a transient ping-timeout during a
        long turn, task still alive) must reopen — preserving run state — not
        recreate. Only a CHANGED IP forces the teardown. (GW-1.)"""
        parked = AsyncMock(spec=GatewayConnection)
        parked.ip = "10.0.0.1"
        parked._parked = True
        parked.is_connected = False
        parked.wait_for_response = AsyncMock(return_value={"ok": True})
        parked._frontend_connections = set()
        pool._connections["user-1"] = parked

        result = await pool.send_rpc("user-1", "r", "agents.list", {}, "10.0.0.1", "tok")

        parked.reopen.assert_awaited_once()
        parked.close.assert_not_called()
        assert result == {"ok": True}

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
        stale_conn.ip = "10.0.0.1"  # matches the resolved ip → adoption is a no-op
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
        # per-method checks on the server (method-scopes.ts).
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
            user_id="test-user",
            ip="10.0.0.1",
            token="tok",
            management_api=mgmt,
        )
        # Owner-keyed fan-out reads recipients from the by-owner-id GSI.
        conn._conn_svc.query_by_owner = AsyncMock(return_value=[("frontend-1", None, "chat")])
        return conn, mgmt

    @pytest.mark.asyncio
    async def test_chat_final_sends_done(self, connection):
        """chat state=final forwards a done message to frontends."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "final", "runId": "run-1", "message": {"content": []}},
            },
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert {"type": "done", "runId": "run-1"} in calls

    @pytest.mark.asyncio
    async def test_chat_final_emits_text_chunk_then_done(self, connection):
        """chat state=final forwards the assistant text as a chunk before the
        done signal. For agent runs the streamed chunks already painted the
        same text (cumulative replace ⇒ visual no-op). For slash commands
        (/help, /approve, etc.) this is the only path that delivers the
        response text to the frontend — without it, slash-command replies
        are silently dropped."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "message": {"content": [{"type": "text", "text": "complete answer"}]},
                },
            },
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert {"type": "chunk", "content": "complete answer", "runId": "run-1"} in calls
        assert {"type": "done", "runId": "run-1"} in calls
        # chunk before done
        assert calls.index({"type": "chunk", "content": "complete answer", "runId": "run-1"}) < calls.index(
            {"type": "done", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_error_sends_error_message(self, connection):
        """chat state=error forwards an error type message."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-e", "error": {"message": "timeout"}},
            },
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert any(c.get("type") == "error" for c in calls)
        assert any("timeout" in c.get("message", "") for c in calls)

    @pytest.mark.asyncio
    async def test_chat_aborted_sends_error_message(self, connection):
        """chat state=aborted forwards a cancellation error."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "aborted", "runId": "run-a"},
            },
        )
        calls = [c.args[1] for c in mgmt.send_message.call_args_list]
        assert any(c.get("type") == "error" for c in calls)

    @pytest.mark.asyncio
    async def test_chat_delta_is_ignored(self, connection):
        """chat state=delta is silently ignored (agent events handle streaming)."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "delta"},
            },
        )
        mgmt.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_events_forwarded_as_is(self, connection):
        """Non-chat, non-agent events are forwarded unchanged for SWR revalidation."""
        conn, mgmt = connection
        msg = {"type": "event", "event": "sessions.updated", "payload": {}}
        await _handle_and_flush(conn, msg)
        mgmt.send_message.assert_called_once_with("frontend-1", msg)


class TestPaperclipSessionFanoutLeak:
    """Paperclip issue/run sessions must NOT reach the web /chat fan-out.

    They carry no member_id, so the owner-keyed fan-out would broadcast them to
    EVERY member of the org — leaking one issue's transcript (and its errors)
    into every member's chat window. Regression test for the reported bug where
    a member saw a background routine's (ADM-455) activity in their own chat.
    """

    @pytest.fixture
    def connection(self):
        mgmt = MagicMock()
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="test-user",
            ip="10.0.0.1",
            token="tok",
            management_api=mgmt,
        )
        # Two org members' chat tabs (member_id from the by-owner-id GSI).
        conn._conn_svc.query_by_owner = AsyncMock(
            return_value=[
                ("frontend-owner", "user_owner", "chat"),
                ("frontend-sibling", "user_sibling", "chat"),
            ]
        )
        return conn, mgmt

    @pytest.mark.asyncio
    async def test_paperclip_issue_chat_final_not_forwarded(self, connection):
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "sessionKey": "agent:friday:paperclip:issue:ADM-455",
                    "message": {"content": [{"type": "text", "text": "sweep done"}]},
                },
            },
        )
        mgmt.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_paperclip_issue_agent_chunk_not_forwarded(self, connection):
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "runId": "run-1",
                    "sessionKey": "agent:friday:paperclip:issue:ADM-455",
                    "data": {"text": "internal routine output"},
                },
            },
        )
        mgmt.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_paperclip_issue_chat_error_not_forwarded(self, connection):
        """An error in the issue run (e.g. context overflow on a retry loop)
        must not bleed into members' chat windows."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "runId": "run-1",
                    "sessionKey": "agent:friday:paperclip:issue:ADM-455",
                    "error": {"message": "Context overflow"},
                },
            },
        )
        mgmt.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_personal_webchat_chat_final_still_forwards(self, connection):
        """Positive control: a real web-chat session (agent:<id>:main) still
        forwards — the fix must not over-drop legitimate chat."""
        conn, mgmt = connection
        await _handle_and_flush(
            conn,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-2",
                    "sessionKey": "agent:friday:main",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            },
        )
        # source=webchat, no member_id -> broadcast to both org tabs.
        assert mgmt.send_message.call_count >= 1


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
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc"}

        conn._emit_status_change("HEALTHY", "Gateway connected")

        await conn._sender.flush()

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
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc", "conn_def"}

        conn._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection lost")

        await conn._sender.flush()

        assert mock_mgmt.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_emit_status_change_handles_send_failure(self):
        """Should not raise if send_message fails for a connection."""
        mock_mgmt = MagicMock()
        mock_mgmt.send_message = MagicMock(side_effect=Exception("gone"))

        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )
        conn._frontend_connections = {"conn_abc"}

        # Should not raise
        conn._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection lost")
        await conn._sender.flush()

    @pytest.mark.asyncio
    async def test_emit_status_change_no_frontends(self):
        """Should be a no-op when no frontend connections exist."""
        mock_mgmt = MagicMock()

        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user_123",
            ip="10.0.1.5",
            token="test-token",
            management_api=mock_mgmt,
        )

        conn._emit_status_change("HEALTHY", "Gateway connected")

        await conn._sender.flush()

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
            user_id="user-1",
            ip="10.0.0.1",
            token="t",
            management_api=mock_management_api,
        )
        # Owner-keyed fan-out reads recipients from the by-owner-id GSI. The
        # session is "agent:main:user-1" → target_member "user-1", so the
        # chat row must carry that member id to receive the demuxed event.
        c._conn_svc.query_by_owner = AsyncMock(return_value=[("conn-1", "user-1", "chat")])
        return c

    @pytest.mark.asyncio
    async def test_done_event_carries_run_id(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-done-1",
                },
            },
        )
        done_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "done"
        ]
        assert len(done_msgs) == 1
        assert done_msgs[0]["runId"] == "run-done-1"

    @pytest.mark.asyncio
    async def test_error_event_carries_run_id(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-err-1",
                    "error": {"message": "model unavailable"},
                },
            },
        )
        error_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "error"
        ]
        assert len(error_msgs) == 1
        assert error_msgs[0]["runId"] == "run-err-1"
        assert error_msgs[0]["message"] == "model unavailable"

    @pytest.mark.asyncio
    async def test_aborted_event_carries_run_id(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-abort-1",
                },
            },
        )
        error_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "error"
        ]
        assert len(error_msgs) == 1
        assert error_msgs[0]["runId"] == "run-abort-1"

    @pytest.mark.asyncio
    async def test_done_without_run_id_raises(self, connection, mock_management_api):
        """runId is required on chat-terminal events. Missing runId is a
        protocol violation — fail loudly rather than silently forwarding
        without it (frontend multi-bubble routing would break)."""
        with pytest.raises(KeyError):
            await _handle_and_flush(
                connection,
                {
                    "type": "event",
                    "event": "chat",
                    "payload": {
                        "state": "final",
                        "sessionKey": "agent:main:user-1",
                    },
                },
            )

    @staticmethod
    def _run_ended_frames(mock_management_api):
        return [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "run_ended"
        ]

    @pytest.mark.asyncio
    async def test_final_emits_run_ended_done(self, connection, mock_management_api):
        """A true terminal final forwards an authoritative run_ended frame
        (outcome=done) IN ADDITION TO the existing done frame, carrying the
        run's runId so the frontend can drain its queue for that run."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-done-1",
                },
            },
        )
        # Existing done frame is unchanged.
        done_msgs = [
            c.args[1] for c in mock_management_api.send_message.call_args_list if c.args[1].get("type") == "done"
        ]
        assert len(done_msgs) == 1
        assert done_msgs[0]["runId"] == "run-done-1"
        # Additional run_ended frame.
        run_ended = self._run_ended_frames(mock_management_api)
        assert len(run_ended) == 1
        assert run_ended[0]["outcome"] == "done"
        assert run_ended[0]["runId"] == "run-done-1"

    @pytest.mark.asyncio
    async def test_error_emits_run_ended_error(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-err-1",
                    "errorMessage": "boom",
                },
            },
        )
        run_ended = self._run_ended_frames(mock_management_api)
        assert len(run_ended) == 1
        assert run_ended[0]["outcome"] == "error"
        assert run_ended[0]["runId"] == "run-err-1"

    @pytest.mark.asyncio
    async def test_aborted_emits_run_ended_aborted(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": "agent:main:user-1",
                    "runId": "run-abort-1",
                },
            },
        )
        run_ended = self._run_ended_frames(mock_management_api)
        assert len(run_ended) == 1
        assert run_ended[0]["outcome"] == "aborted"
        assert run_ended[0]["runId"] == "run-abort-1"

    @pytest.mark.asyncio
    async def test_interim_approval_final_emits_no_run_ended(self, connection, mock_management_api):
        """The interim final that closes a tool-approval bracket is NOT a true
        terminal — no run_ended must be forwarded for it. The post-continuation
        final is the only one that fires run_ended."""
        session_key = "agent:main:user-1"
        connection.mark_run_active(session_key, agent_id="main")

        # 1. Approval requested — pauses the run.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "approval",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {
                        "phase": "requested",
                        "kind": "exec",
                        "status": "pending",
                        "approvalId": "appr-1",
                    },
                },
            },
        )
        # 2. Interim final closing the approval bracket — NO run_ended.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "message": {"content": []},
                },
            },
        )
        assert self._run_ended_frames(mock_management_api) == []

        # 3. True terminal final after the continuation — run_ended fires once.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-2",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                },
            },
        )
        run_ended = self._run_ended_frames(mock_management_api)
        assert len(run_ended) == 1
        assert run_ended[0]["outcome"] == "done"
        assert run_ended[0]["runId"] == "run-2"


class TestActiveRunTracking:
    """Active-run lifecycle: chat.send opens the window, chat-terminal
    events close it. Drives the parked-vs-dead decision when the reader
    loop exits, which in turn drives whether the cold-start
    ProvisioningStepper re-engages on top of a live chat. See
    `connection_pool._on_reader_loop_exit` for the dependent code.
    """

    @pytest.fixture
    def connection(self):
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user-active-run",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        # _handle_message now awaits owner-keyed fan-out; stub the GSI query so
        # these active-run-lifecycle assertions don't touch DDB.
        conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
        return conn

    def test_mark_run_active_records_session(self, connection):
        connection.mark_run_active("agent:main:user-active-run", agent_id="main")
        assert "agent:main:user-active-run" in connection._active_runs
        run = connection._active_runs["agent:main:user-active-run"]
        assert run["agent_id"] == "main"
        assert run["started_at"] > 0
        assert run["last_event_at"] == run["started_at"]
        assert connection.has_active_runs is True

    def test_mark_run_active_is_per_session(self, connection):
        connection.mark_run_active("agent:a:user-1", "a")
        assert "agent:a:user-1" in connection._active_runs
        assert "agent:a:user-2" not in connection._active_runs

    @pytest.mark.asyncio
    async def test_chat_final_clears_active_run(self, connection):
        """chat.final on the matching sessionKey clears the active run."""
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")

        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            },
        )

        assert connection.has_active_runs is False
        assert session_key not in connection._active_runs

    @pytest.mark.asyncio
    async def test_chat_error_clears_active_run(self, connection, monkeypatch):
        """`errorKind: "rate_limit"` drives this through
        `_budget_error_code`'s mid-run reclassification, which reads
        `billing_repo`. Stubbed to return None (no billing row) so this test
        stays deterministic and never touches DynamoDB — this assertion only
        cares about `_active_runs` bookkeeping, not the reclassification
        outcome."""

        async def fake_no_billing_row(owner_id):
            return None

        monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_no_billing_row)

        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")

        await _handle_and_flush(
            connection,
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
            },
        )

        assert connection.has_active_runs is False

    @pytest.mark.asyncio
    async def test_chat_aborted_clears_active_run(self, connection):
        session_key = "agent:main:user-active-run"
        connection.mark_run_active(session_key, agent_id="main")

        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": session_key,
                    "runId": "run-abort",
                },
            },
        )

        assert connection.has_active_runs is False

    @pytest.mark.asyncio
    async def test_side_run_terminal_preserves_primary_active_run(self, connection):
        """A /btw side run shares the primary's sessionKey but owns no window
        (the dispatcher skips mark_run_active for it). Its terminal must NOT
        clear the PRIMARY run's active-run window — otherwise the busy gate
        drops mid-primary-run and a concurrent send races the JSONL write lock.
        Mirrors upstream's per-runId tracking + the clients' separate btw-id
        sets (app-gateway.ts chatSideResultTerminalRuns / tui isLocalBtwRunId).
        """
        session_key = "agent:main:user-1"
        connection.mark_run_active(session_key, agent_id="main")  # primary
        connection.mark_side_run("side-run-9")  # concurrent /btw run

        # The side run finishes first, on the SAME sessionKey but its own runId.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "side-run-9",
                    "message": {"content": [{"type": "text", "text": "aside"}]},
                },
            },
        )
        # Primary window survives; the side id self-cleaned.
        assert session_key in connection._active_runs
        assert connection._is_side_run("side-run-9") is False

        # The PRIMARY run's own terminal clears it normally.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-primary",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                },
            },
        )
        assert session_key not in connection._active_runs

    @pytest.mark.asyncio
    async def test_side_run_approval_does_not_flag_primary_awaiting(self, connection):
        """A side run's approval-requested event must not flag the PRIMARY run
        (shared sessionKey) as awaiting-approval — that would suppress the
        primary's real terminal and wedge its window."""
        session_key = "agent:main:user-1"
        connection.mark_run_active(session_key, "main")
        connection.mark_side_run("side-run-9")

        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "approval",
                    "sessionKey": session_key,
                    "runId": "side-run-9",
                    "data": {"phase": "requested", "kind": "exec", "status": "pending"},
                },
            },
        )
        assert connection._active_runs[session_key]["awaiting_approval"] is False

    @pytest.mark.asyncio
    async def test_approval_bracketed_interim_final_keeps_run_active(self, connection):
        """A tool-approval pause must NOT end the run.

        OpenClaw brackets a pending tool approval like this on the wire (all for
        the same sessionKey, in order):
          1. agent event, stream="approval", data.phase="requested",
             data.status="pending" — the run paused awaiting the user's decision
             (src/agents/embedded-agent-subscribe.handlers.tools.ts →
              emitAgentApprovalEvent / onAgentEvent, infra/agent-events.ts).
          2. chat event, state="final" — the embedded runner closes the *bracket*
             (finalizeLifecycleEvent → emitChatFinal, src/gateway/server-chat.ts),
             but the run CONTINUES after approval with a fresh lifecycle/end.
        Clearing _active_runs on (2) drops the run-active signal across the
        approval (UI "dead air"), so the interim final must be treated as
        non-terminal. Only the post-continuation final (no approval pending)
        is a true terminal.
        """
        session_key = "agent:a:user-1"
        connection.mark_run_active(session_key, "a")

        # 1. Approval requested — the run pauses, not ends.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "approval",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {
                        "phase": "requested",
                        "kind": "exec",
                        "status": "pending",
                        "title": "Command approval requested",
                        "approvalId": "appr-1",
                    },
                },
            },
        )
        assert session_key in connection._active_runs

        # 2. Interim chat final for the approval bracket — run must stay active.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "message": {"content": []},
                },
            },
        )
        assert session_key in connection._active_runs  # still active across approval

        # 3. True terminal final after the continuation — run clears.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": session_key,
                    "runId": "run-2",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                },
            },
        )
        assert session_key not in connection._active_runs

    @pytest.mark.asyncio
    async def test_awaiting_approval_run_cleared_on_error_terminal(self, connection):
        """A run paused on an approval that then ERRORS must still clear.

        `awaiting_approval` only suppresses the interim `final` that closes the
        approval bracket. An `error` is always a true terminal (the run is over
        either way), so it must clear `_active_runs` even while the approval flag
        is set — otherwise a run that dies mid-approval would leak forever and
        the parked-vs-dead teardown in `_on_reader_loop_exit` would mistake the
        dead run for an in-flight one. This is the leak-prevention safety valve.
        """
        session_key = "agent:a:user-1"
        connection.mark_run_active(session_key, "a")

        # Pause on an approval.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "approval",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {
                        "phase": "requested",
                        "kind": "exec",
                        "status": "pending",
                        "approvalId": "appr-1",
                    },
                },
            },
        )
        assert session_key in connection._active_runs
        assert connection._active_runs[session_key]["awaiting_approval"] is True

        # The run errors out WHILE awaiting the approval — still a true terminal.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "errorMessage": "boom",
                    "errorKind": "unknown",
                },
            },
        )
        assert session_key not in connection._active_runs
        assert session_key not in connection._active_runs

    @pytest.mark.asyncio
    async def test_awaiting_approval_run_cleared_on_aborted_terminal(self, connection):
        """A run paused on an approval that is then ABORTED must still clear.

        Same safety valve as the error case: `aborted` is always a true
        terminal, so the approval-pause flag must not keep a dead run alive.
        """
        session_key = "agent:a:user-1"
        connection.mark_run_active(session_key, "a")

        # Pause on an approval.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "approval",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {
                        "phase": "requested",
                        "kind": "exec",
                        "status": "pending",
                        "approvalId": "appr-1",
                    },
                },
            },
        )
        assert connection._active_runs[session_key]["awaiting_approval"] is True

        # The run is aborted WHILE awaiting the approval — still a true terminal.
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "aborted",
                    "sessionKey": session_key,
                    "runId": "run-1",
                },
            },
        )
        assert session_key not in connection._active_runs
        assert session_key not in connection._active_runs

    @pytest.mark.asyncio
    async def test_touch_run_updates_last_event_at(self, connection):
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
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {"text": "tok"},
                },
            },
        )

        bumped = connection._active_runs[session_key]["last_event_at"]
        assert bumped > original

    @pytest.mark.asyncio
    async def test_touch_run_noop_for_unknown_session(self, connection):
        """Untracked sessionKeys (cron / channel) must not create entries."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "sessionKey": "agent:cron-agent:cron:job-7:run:r-1",
                    "runId": "run-cron",
                    "data": {"text": "tok"},
                },
            },
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
            await conn._sender.flush()

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
        await conn._sender.flush()

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


class _FakeTime:
    """Stands in for connection_pool's module-level ``time`` import so the
    backoff arithmetic is driven by a settable clock. Scoped to the module
    namespace (never patches the stdlib) — asyncio's event loop keeps its
    real clock."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class TestPoolAuthBackoff:
    """Auth-shaped connect failures must hold reconnects far longer than the
    5s floor: the gateway locks out a client IP+credential scope for 5
    minutes after 10 failed auths in 60s, and the floor alone allows 12
    attempts/min — enough for a stale token to convert into a full
    AUTH_RATE_LIMITED lockout that bricks the owner's connectivity."""

    def _pool_with_clock(self, monkeypatch, now: float = 1000.0):
        from core.gateway import connection_pool as cp_module

        fake = _FakeTime(now)
        monkeypatch.setattr(cp_module, "time", fake)
        return GatewayConnectionPool(management_api=MagicMock()), fake

    def test_first_auth_failure_holds_base_60s(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", None)
        assert pool._auth_backoff_until["user-1"] == pytest.approx(fake.now + 60)

    def test_exponential_doubling_caps_at_300s(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        for expected in (60, 120, 240, 300, 300):
            pool._record_auth_failure("user-1", None)
            assert pool._auth_backoff_until["user-1"] == pytest.approx(fake.now + expected)

    def test_retry_after_ms_dominates_exponential(self, monkeypatch):
        """The gateway's own retryAfterMs (AUTH_RATE_LIMITED carries the
        remaining lockout) must win over a shorter exponential hold."""
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", 600_000)
        assert pool._auth_backoff_until["user-1"] == pytest.approx(fake.now + 600)

    def test_short_retry_after_ms_does_not_undercut_exponential(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", 1_000)
        assert pool._auth_backoff_until["user-1"] == pytest.approx(fake.now + 60)

    def test_respect_raises_while_hold_active(self, monkeypatch):
        pool, _fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", None)
        with pytest.raises(RuntimeError, match="auth backoff"):
            pool._respect_auth_backoff("user-1")

    def test_respect_passes_after_hold_expires(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", None)
        fake.now += 61
        pool._respect_auth_backoff("user-1")  # must not raise

    def test_respect_noop_for_unknown_user(self, monkeypatch):
        pool, _fake = self._pool_with_clock(monkeypatch)
        pool._respect_auth_backoff("user-1")  # must not raise

    def test_clear_resets_hold_and_escalation(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", None)
        pool._record_auth_failure("user-1", None)
        pool._clear_auth_backoff("user-1")
        pool._respect_auth_backoff("user-1")  # must not raise
        # Escalation restarts from the base hold, not 240s.
        pool._record_auth_failure("user-1", None)
        assert pool._auth_backoff_until["user-1"] == pytest.approx(fake.now + 60)

    @pytest.mark.asyncio
    async def test_ensure_connection_records_auth_failure_and_backs_off(self, monkeypatch):
        from core.gateway.operator_rpc import GatewayConnectAuthError

        pool, _fake = self._pool_with_clock(monkeypatch)
        pool._create_connection = AsyncMock(
            side_effect=GatewayConnectAuthError(
                "Gateway connect failed: rate limited",
                code="AUTH_RATE_LIMITED",
                retry_after_ms=None,
            )
        )
        with pytest.raises(GatewayConnectAuthError):
            await pool.ensure_connection("user-1", "10.0.0.1", "tok")
        assert pool._auth_failures["user-1"] == 1

        # While the hold is active, the connect is suppressed BEFORE dialing.
        with pytest.raises(RuntimeError, match="auth backoff"):
            await pool.ensure_connection("user-1", "10.0.0.1", "tok")
        assert pool._create_connection.await_count == 1

    @pytest.mark.asyncio
    async def test_ensure_connection_success_clears_backoff(self, monkeypatch):
        pool, fake = self._pool_with_clock(monkeypatch)
        pool._record_auth_failure("user-1", None)
        fake.now += 61  # hold expired — attempt allowed again
        conn = MagicMock()
        pool._create_connection = AsyncMock(return_value=conn)
        got = await pool.ensure_connection("user-1", "10.0.0.1", "tok")
        assert got is conn
        assert "user-1" not in pool._auth_backoff_until
        assert "user-1" not in pool._auth_failures


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
        """The chat dispatcher calls mark_run_active BEFORE send_rpc on a cold
        start. It must be safe to issue when no connection exists — the
        dispatcher calls ensure_connection first on the real path, so this just
        guards the no-connection edge (the mark is silently dropped)."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        # Should not raise.
        pool.mark_run_active("user-1", "agent:main:user-1", "main")

    @pytest.mark.asyncio
    async def test_pool_clear_active_run_clears_parked_window(self):
        """The dispatcher calls pool.clear_active_run on the chat.send failure
        path so a send that never reached OpenClaw doesn't leave a stale
        parked-vs-dead entry. It must resolve the owner's connection and pop the
        per-session run."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user-1",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        conn.mark_run_active("agent:main:user-1", "main")
        pool._connections["user-1"] = conn
        assert "agent:main:user-1" in conn._active_runs

        pool.clear_active_run("user-1", "agent:main:user-1")
        assert "agent:main:user-1" not in conn._active_runs

    @pytest.mark.asyncio
    async def test_pool_clear_active_run_noop_when_no_connection(self):
        """clear_active_run runs on the chat.send failure path, which can fire
        before send_rpc ever created the connection. It must be safe to call
        when no connection exists for the owner."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        # Should not raise.
        pool.clear_active_run("user-1", "agent:main:user-1")

    @pytest.mark.asyncio
    async def test_is_user_connected_true_for_parked_with_runs(self):
        """The cold-start phase resolver consults is_user_connected. A
        parked connection with active runs must read as connected so
        /container/status keeps reporting phase=ready and
        ProvisioningStepper does NOT re-engage."""
        pool = GatewayConnectionPool(management_api=MagicMock())
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id="user-parked",
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
        )
        conn._parked = True
        conn.mark_run_active("agent:main:user-parked", "main")
        pool._connections["user-parked"] = conn

        assert pool.is_user_connected("user-parked") is True


@pytest.mark.asyncio
async def test_reopen_failure_rearms_parked_watchdog(monkeypatch):
    """A failed reopen must re-arm the parked watchdog so the connection can
    still escalate parked->dead instead of wedging forever."""
    from core.gateway.connection_pool import GatewayConnection

    conn = GatewayConnection(
        user_id="u",
        ip="10.0.0.1",
        token="t",
        management_api=MagicMock(),
        frontend_connections=set(),
    )
    conn._parked = True
    conn._active_runs = {"agent:1:m": {"last_event_at": time.monotonic(), "agent_id": "1"}}

    async def boom():
        raise TimeoutError("handshake timeout")

    monkeypatch.setattr(conn, "connect", boom)

    with pytest.raises(TimeoutError):
        await conn.reopen()

    assert conn._parked is True
    assert conn._parked_watchdog_task is not None
    assert not conn._parked_watchdog_task.done()

    conn._parked_watchdog_task.cancel()
