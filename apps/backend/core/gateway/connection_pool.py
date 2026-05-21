# backend/core/gateway/connection_pool.py
"""
Persistent WebSocket connection pool to OpenClaw gateway containers.

Maintains one WebSocket per active user. Proxies OpenClaw's native
req/res/event protocol. Background reader task handles incoming messages:
- type=res: resolves pending RPC Futures
- type=event: forwards to user's frontend connections via Management API
"""

import asyncio
import time
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from websockets import connect as ws_connect

from core.observability.metrics import put_metric, gauge
from core.repositories import channel_link_repo

GATEWAY_PORT = 18789  # OpenClaw gateway port (avoid circular import with containers/)

logger = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT = 10  # seconds
_RPC_TIMEOUT = 30  # seconds
_GRACE_PERIOD = 30  # seconds before closing idle connection

# Application-level WebSocket keepalive tuned for long-running LLM turns.
# The websockets library defaults ping_interval=20 / ping_timeout=20 — fine for
# REST-shaped traffic, but the OpenClaw Node event loop legitimately blocks
# >20s during long Bedrock/Chromium turns (Opus turns can run minutes,
# browser-tool turns longer). At the default we'd declare a perfectly-alive
# connection dead, tear down, and storm-reconnect. 300s comfortably exceeds
# the longest realistic single-turn LLM wait while still surfacing genuinely
# dead containers in a bounded window.
_PING_INTERVAL = 20
_PING_TIMEOUT = 300

# Reconnect floor — refuse to reopen a parked or torn-down connection more
# than once every 5s per user. Today the frontend reconnect cadence is
# RECONNECT_DELAYS[]=1s,2s,4s,…; without a server-side floor each retry
# triggered a fresh ws_connect upgrade. A 10s OpenClaw wedge therefore
# burned ~5 simultaneous handshake attempts.
_RECONNECT_FLOOR_S = 5

# How stale `last_event_at` on an active run must be before the parked
# connection counts as "agent appears stuck" for observability. Set by spec
# §6 — we don't ESCALATE on this in this PR (deferred follow-up); we only
# emit `gateway.run.long_idle` metrics + structured logs so the deadlock-
# vs-slow-turn signal is greppable in CloudWatch Insights.
_RUN_LONG_IDLE_S = 300


class GatewayConnection:
    """Single persistent WebSocket to a user's OpenClaw gateway."""

    def __init__(
        self,
        user_id: str,
        ip: str,
        token: str,
        management_api: Any,
        frontend_connections: Set[str],
        conn_member_map: Dict[str, str],
        on_activity: Any = None,
    ) -> None:
        self.user_id = user_id
        self.ip = ip
        self.token = token
        self._management_api = management_api
        self._on_activity = on_activity  # callback(user_id) for idle tracking
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_rpcs: Dict[str, asyncio.Future] = {}
        # Shared reference to the pool's canonical set — NOT a copy.
        self._frontend_connections = frontend_connections
        # Shared reference: connection_id → member_user_id. Used to route
        # streaming events to the specific org member who initiated the chat
        # instead of broadcasting to all members.
        self._conn_member_map = conn_member_map
        self._closed = False
        self._grace_task: Optional[asyncio.Task] = None
        self._billing_tasks: Set[asyncio.Task] = set()
        # Active LLM runs keyed by OpenClaw sessionKey (agent:{agentId}:{member}).
        # Populated by `mark_run_active` when we dispatch chat.send; cleared by
        # `_handle_message` when the matching chat-terminal event arrives.
        # Non-emptiness is the load-bearing signal for the parked-vs-dead
        # teardown decision in `_reader_loop` — see _on_reader_loop_exit.
        self._active_runs: Dict[str, Dict[str, Any]] = {}
        # True after the reader_loop has died with active runs in flight.
        # Means: the OpenClaw process is presumed alive (we just saw it
        # streaming) but our WS to it tore down (e.g. websockets ping
        # timeout during a blocked Node event loop). `is_alive` still
        # returns True so the cold-start phase resolver keeps reporting
        # ready and ProvisioningStepper does not re-engage. Cleared on
        # `_reopen` success.
        self._parked = False
        # Bounded-lifetime watchdog for parked connections. While parked,
        # we presume the container is alive based on active_runs being
        # non-empty + recent last_event_at. If `_RUN_LONG_IDLE_S` elapses
        # without any event, the watchdog escalates parked → dead so a
        # genuinely-deadlocked OpenClaw process can't leak state forever.
        self._parked_watchdog_task: Optional[asyncio.Task] = None

    def _emit_status_change(self, state: str, reason: str) -> None:
        """Push a status_change event to all connected frontend WebSockets."""
        from datetime import timezone

        # Wrap as {type: "event"} so the frontend WS router in useGateway
        # delivers it to onEvent subscribers (unrecognized types are dropped).
        message = {
            "type": "event",
            "event": "status_change",
            "payload": {
                "state": state,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        gone: list[str] = []
        for conn_id in list(self._frontend_connections):
            try:
                if not self._management_api.send_message(conn_id, message):
                    gone.append(conn_id)
            except Exception:
                gone.append(conn_id)
        for conn_id in gone:
            self._frontend_connections.discard(conn_id)
            put_metric("gateway.frontend.prune")
            logger.info("Pruned gone frontend connection %s for user %s", conn_id, self.user_id)

    @property
    def is_connected(self) -> bool:
        """True iff the underlying WS is OPEN and not parked.

        Used by ``send_rpc`` to decide whether to reopen the socket before
        forwarding. ``is_alive`` is the broader liveness signal exposed to
        the cold-start phase resolver.
        """
        if self._parked:
            return False
        if self._ws is None:
            return False
        # websockets v16 uses .state enum; older versions used .closed bool
        state = getattr(self._ws, "state", None)
        if state is not None:
            from websockets.protocol import State

            return state == State.OPEN
        return not getattr(self._ws, "closed", True)

    @property
    def is_alive(self) -> bool:
        """True if the OpenClaw process is presumed responsive.

        Distinct from ``is_connected``: returns True while the WS is parked
        as long as we have at least one active run in flight (the run is
        proof of life — we just saw OpenClaw streaming events). The cold-
        start phase resolver and pool.is_user_connected use this so a
        transient ping-timeout during a long Bedrock turn does NOT flip
        /container/status back to "starting" and re-engage the
        ProvisioningStepper full-screen overlay.
        """
        if self.is_connected:
            return True
        return self._parked and bool(self._active_runs)

    @property
    def has_active_runs(self) -> bool:
        return bool(self._active_runs)

    def mark_run_active(self, session_key: str, agent_id: str | None = None) -> None:
        """Record that a chat.send is being dispatched for *session_key*.

        Called from the chat dispatcher BEFORE the chat.send RPC is forwarded
        to OpenClaw, so the active-run window opens before the first inbound
        event arrives. Idempotent — re-marking refreshes the started_at
        without losing the existing last_event_at.
        """
        now = time.monotonic()
        existing = self._active_runs.get(session_key)
        if existing is not None:
            existing["started_at"] = now
            existing["last_event_at"] = now
            if agent_id:
                existing["agent_id"] = agent_id
            return
        self._active_runs[session_key] = {
            "started_at": now,
            "last_event_at": now,
            "agent_id": agent_id or "",
        }

    def _touch_run(self, session_key: str) -> None:
        """Bump ``last_event_at`` on the active run keyed by *session_key*.

        No-op if no run is active for the key (e.g. cron / channel sessions).
        Cheap enough to call on every inbound agent/chat event.
        """
        run = self._active_runs.get(session_key)
        if run is not None:
            run["last_event_at"] = time.monotonic()

    def _clear_active_run(self, session_key: str) -> None:
        """Pop the run keyed by *session_key* (chat.final / error / aborted)."""
        self._active_runs.pop(session_key, None)

    async def connect(self) -> None:
        """Open WebSocket, complete OpenClaw handshake, verify health, start reader."""
        uri = f"ws://{self.ip}:{GATEWAY_PORT}"
        # Token auth: OpenClaw's trusted-proxy mode explicitly blocks loopback
        # connections (see issue #17761), making it incompatible with our
        # per-user container setup where the local agent must call its own
        # gateway. We use token mode with a per-container shared secret instead.
        # Network isolation (private VPC) provides the transport-level boundary.
        connect_started = time.monotonic()
        # Telemetry: capture which phase of the connect succeeds/fails. When
        # the openclaw process wedges (e.g. EFS NFS deadlock), TCP connect
        # succeeds but the WS upgrade hangs — we want logs that distinguish
        # those two states by elapsed time + which phase failed.
        logger.info(
            "Gateway connect attempt user=%s ip=%s port=%s timeout=%ss",
            self.user_id,
            self.ip,
            GATEWAY_PORT,
            _HANDSHAKE_TIMEOUT,
        )
        try:
            self._ws = await ws_connect(
                uri,
                open_timeout=_HANDSHAKE_TIMEOUT,
                close_timeout=5,
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - connect_started) * 1000)
            logger.warning(
                "Gateway WS upgrade failed user=%s ip=%s elapsed_ms=%d error_class=%s error=%s",
                self.user_id,
                self.ip,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            put_metric(
                "gateway.connection",
                dimensions={"event": "ws_upgrade_failed", "error_class": type(exc).__name__},
            )
            raise
        ws_upgrade_ms = int((time.monotonic() - connect_started) * 1000)

        handshake_started = time.monotonic()
        try:
            await self._handshake()
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - handshake_started) * 1000)
            logger.warning(
                "Gateway openclaw handshake failed user=%s ip=%s ws_upgrade_ms=%d handshake_ms=%d error_class=%s error=%s",
                self.user_id,
                self.ip,
                ws_upgrade_ms,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            put_metric(
                "gateway.connection",
                dimensions={"event": "handshake_failed", "error_class": type(exc).__name__},
            )
            raise
        handshake_ms = int((time.monotonic() - handshake_started) * 1000)

        verify_started = time.monotonic()
        try:
            await self._verify_health()
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - verify_started) * 1000)
            logger.warning(
                "Gateway health verify failed user=%s ip=%s ws_upgrade_ms=%d handshake_ms=%d verify_ms=%d error_class=%s error=%s",
                self.user_id,
                self.ip,
                ws_upgrade_ms,
                handshake_ms,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            put_metric(
                "gateway.connection",
                dimensions={"event": "verify_failed", "error_class": type(exc).__name__},
            )
            raise
        verify_ms = int((time.monotonic() - verify_started) * 1000)

        self._reader_task = asyncio.create_task(self._reader_loop())
        put_metric("gateway.connection", dimensions={"event": "connect"})
        self._emit_status_change("HEALTHY", "Gateway connected")
        total_ms = int((time.monotonic() - connect_started) * 1000)
        logger.info(
            "Gateway connection established user=%s ip=%s total_ms=%d ws_upgrade_ms=%d handshake_ms=%d verify_ms=%d",
            self.user_id,
            self.ip,
            total_ms,
            ws_upgrade_ms,
            handshake_ms,
            verify_ms,
        )

    async def _handshake(self) -> None:
        """Complete OpenClaw 4.5 connect handshake via signed-device auth.

        OpenClaw 4.5 replaced the pre-4.5 "token = admin" model with a
        scoped-auth system (see `src/gateway/method-scopes.ts` in the
        reference). A connect request that provides only `auth.token` with
        no signed device identity gets its self-declared scopes silently
        cleared by the server (`message-handler.ts:438-595`), leaving the
        connection with zero permissions — which breaks `sessions.list`
        (billing poll), `status`, and most of our RPC surface.

        To receive the scopes we actually need, we must:

        1. Hold an Ed25519 keypair (generated at provision time, seed is
           KMS-encrypted in the containers DynamoDB row).
        2. Have the public half pre-registered in the container's
           `devices/paired.json` (also written at provision time).
        3. Sign the canonical v2 payload over the nonce from
           `connect.challenge` and include the signature + public key in
           the `device` field of the connect request.

        This method does all three per connection open. The resulting device
        identity is bound by the signature to the specific gateway token +
        nonce, so captured signatures can't be replayed against different
        tokens or different connects.
        """
        from core.crypto import kms_secrets
        from core.crypto.operator_device import (
            BACKEND_CLIENT_ID,
            BACKEND_CLIENT_MODE,
            BACKEND_OPERATOR_SCOPES,
            BACKEND_ROLE,
            load_operator_device_from_seed,
            sign_connect_request,
        )
        from core.repositories import container_repo

        # Step 1: receive connect.challenge (nonce for the signature)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=_HANDSHAKE_TIMEOUT)
        challenge = json.loads(raw)
        if challenge.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected connect.challenge, got: {challenge.get('event', 'unknown')}")
        nonce = challenge.get("payload", {}).get("nonce") or challenge.get("nonce")
        if not nonce:
            raise RuntimeError(f"connect.challenge missing nonce: {challenge}")

        # Step 2: load the operator device identity for this container.
        # The private seed is KMS-encrypted in DynamoDB and bound by
        # encryption context to the owner_id — a stolen row can't be
        # replayed against a different container.
        container = await container_repo.get_by_owner_id(self.user_id)
        if not container:
            raise RuntimeError(f"No container row for user {self.user_id}")
        enc_seed = container.get("operator_priv_key_enc")
        if not enc_seed:
            raise RuntimeError(
                f"Container row for user {self.user_id} has no operator_priv_key_enc — "
                "re-provision needed (pre-4.5 row without signed-device auth)"
            )
        seed_bytes = kms_secrets.decrypt_bytes(
            enc_seed,
            encryption_context={"owner_id": self.user_id, "purpose": "operator-device-seed"},
        )
        identity = load_operator_device_from_seed(seed_bytes)

        # Step 3: sign the v2 payload with the nonce from the challenge.
        device = sign_connect_request(
            identity=identity,
            token=self.token,
            nonce=nonce,
            scopes=BACKEND_OPERATOR_SCOPES,
        )

        # Step 4: send the connect request with both the token AND the
        # signed device. The scopes field in params must exactly match the
        # scopes in the signed payload — the server re-builds the v2 string
        # from these fields and compares signatures.
        connect_msg = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": 4,
                "maxProtocol": 4,
                "client": {
                    "id": BACKEND_CLIENT_ID,
                    "version": "1.0.0",
                    "platform": "linux",
                    "mode": BACKEND_CLIENT_MODE,
                },
                "role": BACKEND_ROLE,
                "scopes": list(BACKEND_OPERATOR_SCOPES),
                "auth": {"token": self.token},
                "device": device,
                # Opt in to real-time tool events. Without this capability,
                # OpenClaw silently drops stream:"tool" events for this client.
                "caps": ["tool-events"],
            },
        }
        await self._ws.send(json.dumps(connect_msg))

        # Step 5: verify hello-ok
        resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=_HANDSHAKE_TIMEOUT)
        resp = json.loads(resp_raw)
        if not resp.get("ok"):
            err = resp.get("error", {}).get("message", "unknown error")
            raise RuntimeError(f"Gateway connect failed: {err}")

    async def _verify_health(self) -> None:
        """Send health RPC to verify gateway is operational after handshake.

        Called before the reader loop starts, so we read responses directly
        from the WebSocket. Any interleaved events are discarded (no
        in-progress work should exist on a fresh connection).
        """
        req_id = str(uuid.uuid4())
        msg = {"type": "req", "id": req_id, "method": "health", "params": {}}
        await self._ws.send(json.dumps(msg))

        deadline = asyncio.get_event_loop().time() + _HANDSHAKE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                put_metric("gateway.health_check.timeout")
                raise RuntimeError("Gateway health check timed out")

            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            data = json.loads(raw)

            # Skip any events that arrive before our health response
            if data.get("type") != "res" or data.get("id") != req_id:
                continue

            if not data.get("ok"):
                err = data.get("error", {}).get("message", "unknown")
                raise RuntimeError(f"Gateway not healthy: {err}")

            logger.debug("Health check passed for user %s", self.user_id)
            # Write heartbeat to container row. Future watchdog can detect
            # `last_gateway_handshake_at` going stale (e.g. EFS NFS deadlock
            # leaves TCP listener bound but no JS runs, so heartbeats stop
            # while ECS still reports the task RUNNING). Best-effort — never
            # let a heartbeat write failure break a successful health check.
            try:
                from core.repositories import container_repo

                await container_repo.update_fields(
                    self.user_id,
                    {"last_gateway_handshake_at": datetime.now(timezone.utc).isoformat()},
                )
            except Exception as exc:
                logger.debug(
                    "heartbeat write failed user=%s error=%s",
                    self.user_id,
                    exc,
                )
            return

    async def send_rpc(self, req_id: str, method: str, params: dict) -> None:
        """Send {type: req} on the gateway WebSocket."""
        msg = {"type": "req", "id": req_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))

    async def wait_for_response(self, req_id: str, timeout: float = _RPC_TIMEOUT) -> Any:
        """Wait for the matching res message. Returns payload or raises."""
        future = asyncio.get_running_loop().create_future()
        self._pending_rpcs[req_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_rpcs.pop(req_id, None)
            raise
        finally:
            self._pending_rpcs.pop(req_id, None)

    @staticmethod
    def _transform_agent_event(payload: dict) -> dict | None:
        """Extract streaming text, thinking, or tool events from an OpenClaw agent event.

        Agent events fire for every LLM token (no 150ms throttle).
        - ``stream: "assistant"`` events with text are forwarded as chunks.
        - ``stream: "reasoning"`` / ``"thinking"`` events are forwarded for real-time thinking.
        - ``stream: "tool"`` events are forwarded as tool_start/tool_end/tool_error.

        The OpenClaw ``runId`` on the source payload is propagated onto every
        transformed event so the frontend can route chunks/tools to the
        correct per-run assistant bubble (multi-bubble rendering).
        """
        stream = payload.get("stream")
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        transformed: dict | None = None

        if stream == "assistant":
            text = data.get("text", "")
            if text:
                transformed = {"type": "chunk", "content": text}

        elif stream in ("reasoning", "thinking"):
            text = data.get("text", "")
            if text:
                transformed = {"type": "thinking", "content": text}

        elif stream == "tool":
            phase = data.get("phase", "")
            name = data.get("name", "")
            if name:
                tool_call_id = data.get("toolCallId", "")
                if phase == "start":
                    transformed = {"type": "tool_start", "tool": name}
                    if tool_call_id:
                        transformed["toolCallId"] = tool_call_id
                    if "args" in data:
                        transformed["args"] = data["args"]
                elif phase == "result":
                    is_error = bool(data.get("isError"))
                    transformed = {
                        "type": "tool_error" if is_error else "tool_end",
                        "tool": name,
                    }
                    if tool_call_id:
                        transformed["toolCallId"] = tool_call_id
                    if "result" in data:
                        transformed["result"] = data["result"]
                    if data.get("meta"):
                        transformed["meta"] = data["meta"]

        if transformed is None:
            return None

        # OpenClaw emits runId on every agent event we forward (assistant/
        # reasoning/thinking/tool streams), so treat it as a required field.
        # Frontend routes chunks/tools to a per-run assistant bubble keyed by
        # this id — a missing runId would silently break multi-bubble rendering.
        transformed["runId"] = payload["runId"]
        return transformed

    @staticmethod
    def _extract_thinking_text(payload: dict) -> str | None:
        """Extract thinking/reasoning text from a chat event's message.content field."""
        msg = payload.get("message")
        if not isinstance(msg, dict):
            return None
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        thinking_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking") or block.get("text") or ""
                if text:
                    thinking_parts.append(text)
        return "\n\n".join(thinking_parts) if thinking_parts else None

    @staticmethod
    def _extract_assistant_text(payload: dict) -> str | None:
        """Extract assistant text from a chat-final's message.content field.

        Slash commands (/help, /whoami, /approve, etc.) reply via chat.final
        without an agent stream — the response text only exists here. For
        normal agent runs the chunks already painted the same text, but
        re-asserting it via cumulative replacement is a no-op visually
        (OpenClaw's flushBufferedChatDeltaIfNeeded backstop guarantees the
        last streamed chunk equals the final message).
        """
        msg = payload.get("message")
        if not isinstance(msg, dict):
            return None
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)
        return "".join(text_parts) if text_parts else None

    def _forward_to_frontends(self, message: dict, target_member_id: str | None = None) -> None:
        """Send a message to registered frontend connections.

        When *target_member_id* is set, only connections belonging to that
        member receive the message (per-member event routing for orgs).
        When ``None``, broadcast to all connections (status changes, etc.).
        """
        all_conns = list(self._frontend_connections)
        if not all_conns:
            msg_type = message.get("type", "?")
            logger.warning(
                "[%s] forward: no frontend connections for msg type=%s target=%s",
                self.user_id,
                msg_type,
                target_member_id or "broadcast",
            )
            return
        gone: list[str] = []
        delivered = 0
        skipped = 0
        for conn_id in all_conns:
            if target_member_id is not None:
                # Case-insensitive: OpenClaw lowercases sessionKeys
                # internally, so the member_id parsed from the event is
                # lowercase, but conn_member_map stores the original
                # Clerk user_id (mixed case).
                member = self._conn_member_map.get(conn_id, "")
                if member.lower() != target_member_id.lower():
                    skipped += 1
                    continue
            try:
                if not self._management_api.send_message(conn_id, message):
                    gone.append(conn_id)
                else:
                    delivered += 1
            except Exception:
                logger.warning("Failed to forward message to %s", conn_id)
                gone.append(conn_id)
        for conn_id in gone:
            self._frontend_connections.discard(conn_id)
            put_metric("gateway.frontend.prune")
            logger.info("Pruned gone frontend connection %s for user %s", conn_id, self.user_id)
        # Log delivery summary for non-chunk messages (chunks are too frequent)
        msg_type = message.get("type", "?")
        if msg_type not in ("chunk",):
            logger.info(
                "[%s] forward type=%s target=%s delivered=%d skipped=%d gone=%d total=%d",
                self.user_id,
                msg_type,
                target_member_id or "broadcast",
                delivered,
                skipped,
                len(gone),
                len(all_conns),
            )

    def _record_usage_from_session(self, payload: dict) -> None:
        """Record usage after a billable event by resolving the session key
        to a member_id and querying session tokens.

        Triggered from the `agent` event lifecycle/end branch below (not
        from chat.final, which only fires for webchat and doesn't exist for
        channel-driven runs).
        """
        session_key = payload.get("sessionKey", "")
        if not session_key:
            logger.warning(
                "No sessionKey in billable event for user %s — cannot record usage",
                self.user_id,
            )
            return

        parsed = _parse_session_key(session_key)
        if not parsed:
            logger.warning(
                "Malformed sessionKey %r for user %s — cannot record usage",
                session_key,
                self.user_id,
            )
            return

        async def _resolve_then_record():
            try:
                member_id = await self._resolve_member_from_session(parsed)
                await self._fetch_and_record_usage(session_key, member_id)
            except Exception:
                # Don't let billing failures crash the gateway reader. Log
                # with full traceback so DynamoDB outages and similar
                # transient failures are observable.
                logger.exception(
                    "Failed to resolve+record usage for user %s session %s",
                    self.user_id,
                    session_key,
                )

        task = asyncio.create_task(_resolve_then_record())
        # Track the in-flight task so close() can await it on shutdown.
        # Without this, scale-to-zero teardown can cancel pending billing
        # writes mid-flight, silently losing the last few usage events.
        self._billing_tasks.add(task)
        task.add_done_callback(self._billing_tasks.discard)

    async def _resolve_member_from_session(self, parsed: dict) -> str:
        """Map a parsed session key to the Clerk member_id.

        Falls back to self.user_id (the owner) if no per-member attribution
        is available (unlinked DM, group, channel, webchat-personal, unknown).
        """
        if parsed.get("source") == "dm":
            link = await channel_link_repo.get_by_peer(
                owner_id=self.user_id,
                provider=parsed["channel"],
                agent_id=parsed["agent_id"],
                peer_id=parsed["peer_id"],
            )
            if link:
                return link.get("member_id", self.user_id)
            return self.user_id

        if parsed.get("source") == "webchat" and parsed.get("member_id"):
            return parsed["member_id"]

        return self.user_id

    async def _fetch_and_record_usage(self, session_key: str, member_user_id: str) -> None:
        """Fetch session token counts via RPC and record usage."""
        try:
            # Query sessions.list to get token data for this session
            req_id = str(uuid.uuid4())
            await self.send_rpc(req_id, "sessions.list", {})
            result = await self.wait_for_response(req_id, timeout=10)

            if not isinstance(result, dict):
                logger.warning(
                    "sessions.list returned non-dict for user %s: type=%s",
                    self.user_id,
                    type(result).__name__,
                )
                return

            # Find our session in the list.
            # sessions.list returns "key" (not "sessionKey"), and chat.final
            # lowercases the session key, so compare case-insensitively.
            sessions = result.get("sessions", [])
            session_key_lower = session_key.lower()
            logger.info(
                "sessions.list returned %d sessions for user %s, looking for %s",
                len(sessions),
                self.user_id,
                session_key,
            )
            session = None
            for s in sessions:
                s_key = s.get("key", "")
                if s_key.lower() == session_key_lower:
                    session = s
                    break

            if not session:
                available_keys = [s.get("key", "?") for s in sessions[:5]]
                logger.warning(
                    "Session %s not found for user %s. Available: %s",
                    session_key,
                    self.user_id,
                    available_keys,
                )
                return

            input_tokens = int(session.get("inputTokens", 0) or 0)
            output_tokens = int(session.get("outputTokens", 0) or 0)
            cache_read = int(session.get("cacheRead", 0) or 0)
            cache_write = int(session.get("cacheWrite", 0) or 0)
            model = session.get("model") or "unknown"

            logger.info(
                "Session %s tokens: in=%d out=%d cache_r=%d cache_w=%d model=%s",
                session_key,
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                model,
            )

            if input_tokens <= 0 and output_tokens <= 0:
                logger.warning(
                    "Zero tokens in session %s for user %s — session keys: %s",
                    session_key,
                    self.user_id,
                    list(session.keys()),
                )
                return

            # Record usage directly
            try:
                from core.services.usage_service import record_usage

                await record_usage(
                    owner_id=self.user_id,
                    user_id=member_user_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                )
                logger.info(
                    "Recorded usage for user %s (member=%s): model=%s in=%d out=%d",
                    self.user_id,
                    member_user_id,
                    model,
                    input_tokens,
                    output_tokens,
                )
            except Exception:
                logger.exception("Failed to record usage for user %s", self.user_id)

            # Card-3 (bedrock_claude) deducts credits in addition to the
            # legacy usage_service path. Other cards skip.
            # Synchronous so the next chat sees the updated balance. The
            # ledger is owner-scoped — _maybe_deduct_credits keys on
            # self.user_id (the container owner), so org members share
            # one pooled balance.
            try:
                await self._maybe_deduct_credits(
                    chat_session_id=session_key,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                )
            except Exception:
                logger.exception(
                    "Failed to deduct credits for user %s session %s",
                    self.user_id,
                    session_key,
                )

        except Exception:
            put_metric("chat.session_usage.fetch.error")
            logger.exception("Failed to fetch session usage for user %s", self.user_id)

    async def _maybe_deduct_credits(
        self,
        *,
        chat_session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Card-3 (bedrock_claude) credit deduct on chat completion.

        Per spec §6.3: card-3 users prepay for Claude inference; we deduct
        from credit_ledger after each session ends. Cards 1 + 2 skip this
        entirely. Unknown model_id (e.g. a new Claude model not yet in
        bedrock_pricing) is logged + skipped — no overdraft, no error to
        the user; the operator updates bedrock_pricing.py and the next
        chat deducts correctly.

        Markup is applied here (1.4x raw) per the spec's pricing model.

        Both provider_choice and the credit ledger key are owner-scoped —
        ``self.user_id`` IS the container owner (org_id for org members,
        user_id for personal). Org members sharing a container all draw
        from one pooled balance.
        """
        from core.billing.bedrock_pricing import (
            UnknownModelError,
            cost_microcents,
        )
        from core.repositories import billing_repo
        from core.services import credit_ledger

        account = await billing_repo.get_by_owner_id(self.user_id)
        if not account or account.get("provider_choice") != "bedrock_claude":
            return

        # Bedrock model ids may be passed with or without a provider prefix
        # (e.g. "amazon-bedrock/anthropic.claude-sonnet-4-6"). Strip the
        # prefix before looking up rates.
        bare_model = model.split("/", 1)[-1] if "/" in model else model

        try:
            raw = cost_microcents(
                model_id=bare_model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_write_tokens=int(cache_write_tokens or 0),
            )
        except UnknownModelError:
            logger.warning(
                "Unknown Bedrock model %r in chat completion for user %s — skipping deduct",
                bare_model,
                self.user_id,
            )
            put_metric("credit.deduct.unknown_model", dimensions={"model": bare_model})
            return

        # Integer math — avoid IEEE-754 drift on the multiply.
        # `raw * 1.4` is not exact (10500 * 1.4 = 14699.999...) so we use
        # `raw * 14 // 10` instead. Spec §6.3 mandates microcents-as-int
        # throughout the credit ledger.
        marked_up = raw * 14 // 10
        await credit_ledger.deduct(
            self.user_id,
            amount_microcents=marked_up,
            chat_session_id=chat_session_id,
            raw_cost_microcents=raw,
            markup_multiplier=1.4,
        )
        logger.info(
            "Deducted credits for owner %s session %s: raw=%d marked_up=%d model=%s",
            self.user_id,
            chat_session_id,
            raw,
            marked_up,
            bare_model,
        )

    def _handle_message(self, data: dict) -> None:
        """Route an incoming gateway message.

        Event routing:
        - ``agent`` events (unthrottled): token-by-token streaming via
          ``_transform_agent_event``.
        - ``chat`` events: only terminal states (final/error/aborted). Delta
          states are ignored because agent events provide the same data without
          the 150ms throttle.
        - Other events: forwarded as-is for SWR revalidation.
        """
        msg_type = data.get("type")

        if msg_type == "res":
            req_id = data.get("id")
            future = self._pending_rpcs.get(req_id)
            if future and not future.done():
                if data.get("ok"):
                    future.set_result(data.get("payload", {}))
                else:
                    # Serialize the full error object so details/issues survive
                    err_obj = data.get("error", {})
                    if isinstance(err_obj, dict):
                        err_msg = json.dumps(err_obj)
                    else:
                        err_msg = str(err_obj or "RPC call rejected")
                    future.set_exception(RuntimeError(err_msg))
            return

        if msg_type == "event":
            event_name = data.get("event", "")
            payload = data.get("payload", {})

            # Extract per-member routing target from sessionKey. OpenClaw
            # includes sessionKey in agent/chat event payloads (set in
            # server-chat.ts:865). For org webchat sessions the key is
            # "agent:{agentId}:{userId}" — _parse_session_key extracts
            # member_id so we can route events to the specific member
            # who initiated the chat instead of broadcasting to all org
            # members. Falls back to None (broadcast) for personal
            # sessions, channel sessions, or missing keys.
            session_key = ""
            if isinstance(payload, dict):
                session_key = payload.get("sessionKey", "")
            parsed_key = _parse_session_key(session_key) if session_key else {}
            target_member = parsed_key.get("member_id")
            # Channel-originated events (Telegram, Discord, Slack DMs, groups,
            # rooms) don't need to be forwarded to the web UI — OpenClaw's
            # channel plugin already delivers them directly to the platform.
            # Without this gate, channel responses leak into the web chat and
            # overwrite or intermix with the user's web conversation.
            session_source = parsed_key.get("source", "")
            is_channel_session = session_source in ("dm", "group", "channel")
            is_cron_session = session_source == "cron"

            # Drop noisy periodic events (health, tick) entirely — they are
            # OpenClaw-internal keep-alives and should never reach frontends.
            if event_name in ("health", "tick"):
                logger.debug(
                    "Gateway heartbeat for %s: event=%s",
                    self.user_id,
                    event_name,
                )
                return

            # Bump the active-run liveness timestamp for this session_key.
            # No-op when no run is tracked for the key (cron/channel sessions,
            # missed mark_run_active call) so it is cheap to call eagerly.
            if session_key:
                self._touch_run(session_key)
            if event_name != "agent":
                state = payload.get("state", "") if isinstance(payload, dict) else ""
                logger.info(
                    "[%s] gateway event=%s state=%s sessionKey=%s target=%s",
                    self.user_id,
                    event_name,
                    state,
                    session_key[:60] if session_key else "-",
                    target_member or "broadcast",
                )

            if event_name == "agent":
                # Unthrottled agent events -- smooth token-by-token streaming
                stream = payload.get("stream", "")
                # Exclude token-level streams (assistant, reasoning, thinking)
                # from INFO logs — each fires per LLM token, so logging would
                # blow up CloudWatch volume on long responses.
                if stream not in ("assistant", "reasoning", "thinking", ""):
                    data = payload.get("data", {})
                    logger.info(
                        "Agent event for user %s: stream=%s phase=%s name=%s keys=%s",
                        self.user_id,
                        stream,
                        data.get("phase", ""),
                        data.get("name", ""),
                        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                    )
                # Billing: lifecycle/end fires once per completed agent run
                # for BOTH webchat and channel-driven runs (webchat's chat.final
                # only fires for webchat, so we use lifecycle/end instead).
                # Billing runs for ALL sessions (web + channel).
                if (
                    stream == "lifecycle"
                    and isinstance(payload.get("data"), dict)
                    and payload["data"].get("phase") == "end"
                ):
                    self._record_usage_from_session(payload)
                # Skip forwarding channel and cron events to the web UI.
                # Cron jobs run in isolated sessions inside OpenClaw — their
                # streaming events must not leak into a user's active web chat
                # (a cron "done" would terminate the user's streaming session).
                if is_channel_session or is_cron_session:
                    return
                transformed = self._transform_agent_event(payload)
                if transformed:
                    # Tag with agent_id so the frontend can route messages
                    # to the correct agent conversation and prevent cross-
                    # agent leakage when switching agents mid-stream.
                    event_agent_id = parsed_key.get("agent_id")
                    if event_agent_id:
                        transformed["agent_id"] = event_agent_id
                    self._forward_to_frontends(transformed, target_member)

            elif event_name == "chat":
                # Skip forwarding channel and cron chat events to the web UI.
                if is_channel_session or is_cron_session:
                    return
                # Chat events -- only terminal states.
                # Delta states are skipped; agent events handle streaming.
                state = payload.get("state", "")
                # Tag all chat messages with agent_id so the frontend can
                # route responses to the correct agent conversation.
                event_agent_id = parsed_key.get("agent_id")
                # runId is a required field on every chat-terminal event we
                # forward: the frontend keys its per-run assistant bubble on
                # it. OpenClaw guarantees it on final/error/aborted (server-
                # chat.ts `chat` event emitter), so no defensive guard here.
                # Delta/unknown states never forward, so they don't need it.
                if state not in ("final", "error", "aborted"):
                    return
                # Terminal state for this session_key — clear the active-run
                # entry so subsequent reader_loop exits make the dead-vs-
                # parked decision based on still-in-flight runs only.
                if session_key:
                    self._clear_active_run(session_key)
                run_id = payload["runId"]
                if state == "final":
                    put_metric("chat.message.count")
                    # Deliver thinking from content blocks for models that
                    # batch reasoning into the final message (reasoningLevel
                    # not set to "stream"). Once reasoningLevel:"stream" is
                    # enabled on the session, this will typically be empty
                    # because thinking streamed via agent events already.
                    thinking_text = self._extract_thinking_text(payload)
                    if thinking_text:
                        fwd: dict = {"type": "thinking", "content": thinking_text}
                        if event_agent_id:
                            fwd["agent_id"] = event_agent_id
                        fwd["runId"] = run_id
                        self._forward_to_frontends(fwd, target_member)
                    # Forward the assistant text from chat.final. For agent
                    # runs the streamed chunks already painted the same text
                    # (chunks use cumulative replacement), so this is a visual
                    # no-op. For slash commands (/help, /approve, etc.) there
                    # is no agent stream — this is the only path that delivers
                    # the response text to the frontend.
                    text_content = self._extract_assistant_text(payload)
                    if text_content:
                        fwd = {"type": "chunk", "content": text_content}
                        if event_agent_id:
                            fwd["agent_id"] = event_agent_id
                        fwd["runId"] = run_id
                        self._forward_to_frontends(fwd, target_member)
                    fwd = {"type": "done"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    self._forward_to_frontends(fwd, target_member)
                elif state == "error":
                    # OpenClaw's `chat` terminal error event carries the
                    # human-readable text in `errorMessage` and a coarse,
                    # closed-enum classification in `errorKind` (see
                    # upstream src/gateway/protocol/schema/logs-chat.ts:
                    # ChatEventSchema). `errorKind` is one of:
                    #   refusal | timeout | rate_limit | context_length | unknown
                    # We forward both so the frontend can render a typed
                    # error card with tailored copy/actions instead of a
                    # generic "Agent run failed" block. The legacy `error`
                    # key is read as a defensive fallback in case some
                    # older path still emits it.
                    err_msg = payload.get("errorMessage")
                    err_kind = payload.get("errorKind")
                    if not err_msg:
                        legacy_err = payload.get("error")
                        if isinstance(legacy_err, dict):
                            err_msg = legacy_err.get("message")
                        elif legacy_err:
                            err_msg = str(legacy_err)
                    if not err_msg:
                        err_msg = "Agent run failed"
                    put_metric(
                        "chat.error",
                        dimensions={"reason": err_kind or "agent_error"},
                    )
                    fwd = {"type": "error", "message": err_msg}
                    if err_kind:
                        fwd["code"] = err_kind
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    self._forward_to_frontends(fwd, target_member)
                elif state == "aborted":
                    put_metric("chat.error", dimensions={"reason": "aborted"})
                    fwd = {"type": "error", "message": "Agent run was cancelled"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    self._forward_to_frontends(fwd, target_member)

            else:
                # Forward other events as-is for SWR revalidation (broadcast to all)
                self._forward_to_frontends(data)
            return

    async def _reader_loop(self) -> None:
        """Background task: read all messages from gateway WebSocket."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                    self._handle_message(data)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message from gateway for user %s", self.user_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if self._closed:
                return
            self._on_reader_loop_exit(e)

    def _on_reader_loop_exit(self, exc: BaseException) -> None:
        """Decide between PARKED and DEAD when the reader loop dies.

        The websockets library treats a >ping_timeout silent peer as a
        connection error and tears the reader down. Historically this code
        path unconditionally rejected every pending RPC and emitted
        GATEWAY_DOWN, which during a long Bedrock turn would (a) fail
        in-flight callers, (b) flip /container/status from ready to
        starting, (c) re-engage ProvisioningStepper on top of the live
        chat. With `_PING_TIMEOUT=300` that should almost never fire
        during a healthy turn, but the defense-in-depth split below means
        a genuinely-misbehaving timeout still lands us in PARKED (alive,
        will reconnect lazily) rather than DEAD when a chat is in flight.

        PARKED: active runs in flight → presume OpenClaw is alive, keep
                `_pending_rpcs` (the in-flight chat.send was already acked,
                non-chat RPCs will time out at `_RPC_TIMEOUT` on their own),
                emit `GATEWAY_DOWN_LLM_BUSY`, spawn a watchdog that
                escalates to DEAD after `_RUN_LONG_IDLE_S` of total
                event silence.

        DEAD:   no active runs → today's behavior. Reject pending RPCs,
                emit `GATEWAY_DOWN_DEAD`. The pool will lazily reconnect
                on the next outbound message subject to the reconnect
                floor.
        """
        if self._active_runs:
            self._parked = True
            put_metric(
                "gateway.connection",
                dimensions={"event": "parked", "active_runs": str(len(self._active_runs))},
            )
            put_metric("gateway.parked.enter")
            # Structured log so CloudWatch Insights can correlate parked
            # teardown with the longest-idle run age. Aggregations over
            # this signal feed the deferred deadlock-watchdog tuning work.
            now = time.monotonic()
            ages_ms = [int((now - run["last_event_at"]) * 1000) for run in self._active_runs.values()]
            logger.warning(
                "Gateway reader exited with %d active run(s) — parking user=%s "
                "error_class=%s max_last_event_age_ms=%d sessions=%s",
                len(self._active_runs),
                self.user_id,
                type(exc).__name__,
                max(ages_ms) if ages_ms else 0,
                list(self._active_runs.keys()),
            )
            self._emit_status_change(
                "GATEWAY_DOWN_LLM_BUSY",
                "Agent is still working — keeping the run alive",
            )
            if self._parked_watchdog_task is None or self._parked_watchdog_task.done():
                self._parked_watchdog_task = asyncio.create_task(self._parked_watchdog())
            return

        # No active runs — dead-for-real path.
        put_metric("gateway.connection", dimensions={"event": "error"})
        logger.error(
            "Gateway reader loop error for user %s: %s error_class=%s",
            self.user_id,
            exc,
            type(exc).__name__,
        )
        self._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection lost")
        for req_id, future in list(self._pending_rpcs.items()):
            if not future.done():
                future.set_exception(RuntimeError("Gateway connection lost"))
        self._pending_rpcs.clear()

    async def _parked_watchdog(self) -> None:
        """Bounded-lifetime guard for parked connections.

        Sleeps until the earliest active run has been silent for
        `_RUN_LONG_IDLE_S` seconds. On wake:
          - If the connection was already reopened or closed, exit (the
            reopen() / close() paths cancel us).
          - If active runs still exist and they're ALL silent past the
            threshold, escalate to DEAD: reject pending RPCs, clear
            active_runs, emit `GATEWAY_DOWN_DEAD`. The next outbound
            message triggers a fresh reconnect from the pool.
          - Otherwise (some run saw activity since this watchdog was
            scheduled — happens if a fresh chat.send marked a new run
            during the parked window), reschedule with the updated
            deadline.

        Emits `gateway.run.long_idle` per escalation so the deadlock-
        vs-slow-turn ratio is observable without parsing log lines.
        """
        try:
            while True:
                if self._closed or not self._parked:
                    return
                if not self._active_runs:
                    return
                now = time.monotonic()
                # Time until the youngest run crosses the long-idle line.
                # Watchdog escalates only when ALL runs are stale — if any
                # one is still receiving events, the conn is alive.
                youngest_age = min(now - run["last_event_at"] for run in self._active_runs.values())
                wait = _RUN_LONG_IDLE_S - youngest_age
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue

                # All runs silent past the threshold — escalate.
                put_metric(
                    "gateway.run.long_idle",
                    dimensions={"active_runs": str(len(self._active_runs))},
                )
                logger.error(
                    "Parked connection long-idle for user=%s active_runs=%d — escalating to dead. Sessions: %s",
                    self.user_id,
                    len(self._active_runs),
                    list(self._active_runs.keys()),
                )
                self._active_runs.clear()
                self._parked = False
                self._emit_status_change(
                    "GATEWAY_DOWN_DEAD",
                    "Agent appears stuck — try restarting the gateway",
                )
                for req_id, future in list(self._pending_rpcs.items()):
                    if not future.done():
                        future.set_exception(RuntimeError("Gateway connection lost"))
                self._pending_rpcs.clear()
                return
        except asyncio.CancelledError:
            return

    async def reopen(self) -> None:
        """Re-establish the underlying WS while preserving run state.

        Used by the pool when a parked connection needs to forward a new
        outbound message. Differs from `connect()`: we keep `_active_runs`
        and `_pending_rpcs` (so chat.history / sessions.list polls that
        were issued before the WS hiccup can still resolve on the fresh
        socket), but rebuild the WS / handshake / health-verify chain
        and start a fresh reader_loop.
        """
        # Cancel the parked watchdog before clearing _parked so the
        # watchdog body sees the right state.
        if self._parked_watchdog_task and not self._parked_watchdog_task.done():
            self._parked_watchdog_task.cancel()
        self._parked_watchdog_task = None
        # Old WS is already torn down (reader_loop exited); just drop the ref.
        self._ws = None
        await self.connect()
        self._parked = False
        put_metric("gateway.parked.reopen")
        logger.info(
            "Reopened parked gateway connection user=%s active_runs=%d",
            self.user_id,
            len(self._active_runs),
        )

    @property
    def has_frontend_connections(self) -> bool:
        return len(self._frontend_connections) > 0

    async def close(self) -> None:
        """Shut down: cancel reader, close WebSocket."""
        self._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection closed")
        self._closed = True
        # Wait for any in-flight billing-resolver tasks to finish writing
        # so scale-to-zero teardown doesn't drop the last few usage events.
        if self._billing_tasks:
            try:
                await asyncio.wait(
                    self._billing_tasks,
                    timeout=5.0,
                )
            except Exception:
                logger.exception(
                    "Error waiting for in-flight billing tasks during close for user %s",
                    self.user_id,
                )
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
        if self._parked_watchdog_task and not self._parked_watchdog_task.done():
            self._parked_watchdog_task.cancel()
            try:
                await self._parked_watchdog_task
            except asyncio.CancelledError:
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        # Reject pending RPCs
        for req_id, future in list(self._pending_rpcs.items()):
            if not future.done():
                future.set_exception(RuntimeError("Connection closed"))
        self._pending_rpcs.clear()
        # Drop active-run state. close() means we don't expect to talk to
        # this OpenClaw process again under this connection instance.
        self._active_runs.clear()
        self._parked = False


class GatewayConnectionPool:
    """Pool of persistent gateway connections, one per active user."""

    def __init__(
        self,
        management_api: Any,
    ) -> None:
        self._management_api = management_api
        self._connections: Dict[str, GatewayConnection] = {}
        self._frontend_connections: Dict[str, Set[str]] = {}  # owner_id -> set of conn_ids
        self._conn_member_map: Dict[str, str] = {}  # connection_id -> member_user_id
        self._lock = asyncio.Lock()
        self._grace_tasks: Dict[str, asyncio.Task] = {}
        # Per-user reconnect attempt timestamps for the `_RECONNECT_FLOOR_S`
        # storm guard. Tracks the start of the *most recent* reconnect (not
        # success / failure) so a single bad container can't burn the
        # backend's open-handshake budget at 1s cadence.
        self._last_reconnect_at: Dict[str, float] = {}

    def is_user_connected(self, user_id: str) -> bool:
        """Whether the pool has a live, healthy gateway connection for this user.

        Used by the cold-start phase resolver in the container router. True
        means EITHER the WS is fully open OR the connection is parked with
        at least one active run in flight (presumed alive — OpenClaw was
        streaming events recently, the WS just hiccupped). The parked case
        is what keeps /container/status reporting `phase: "ready"` during
        long Bedrock turns so the ProvisioningStepper doesn't re-engage
        on top of an active chat.
        """
        conn = self._connections.get(user_id)
        return conn is not None and conn.is_alive

    def mark_run_active(
        self,
        user_id: str,
        session_key: str,
        agent_id: str | None = None,
    ) -> None:
        """Public entry point for the chat dispatcher.

        Records that a chat.send is being issued for *session_key* on the
        per-user gateway connection. Silently no-ops when no connection
        exists for *user_id* yet (the chat dispatcher will create one on
        the same code path); the next inbound event keyed on this
        session_key will populate ``_active_runs`` once the connection
        is up. Idempotent.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.mark_run_active(session_key, agent_id)

    async def gate_chat(self, *, user_id: str, owner_id: str) -> dict:
        """Pre-chat hard-stop for card-3 (bedrock_claude) users.

        Two layers (audit M1 expanded the second one):

        1. **Subscription status.** The billing row's ``subscription_status``
           must be ``active`` or ``trialing``. Cards 1 + 2 used to bypass
           the gate entirely because their LLM cost lives on the user's
           own provider account — but the *container* itself is on us, so
           a canceled subscription should still block chat. Without this,
           an org/personal user whose ``subscription.deleted`` webhook
           landed could keep talking through the gateway indefinitely on
           cards 1 + 2.
        2. **Card-3 balance.** When ``provider_choice`` is
           ``bedrock_claude``, the credit balance must be > 0. Cards 1
           + 2 don't draw from prepaid credits, so they're not gated
           on balance.

        Both layers key on ``owner_id`` (org_id for org context, user_id
        for personal). ``user_id`` is the chatting member, retained on
        the signature for logging / future per-user telemetry.

        Returns:
            ``{"blocked": False}`` when chat may proceed.
            ``{"blocked": True, "code": <code>, "message": ...}``
            when the chat must NOT be forwarded to OpenClaw.

        Reads use ``ConsistentRead=True`` so a top-up that just landed
        via Stripe webhook unblocks the next message immediately.
        """
        from core.repositories import billing_repo
        from core.services import credit_ledger
        from core.services.provision_gate import is_subscription_active

        # --- Layer 1: subscription status ----------------------------
        # Anything that isn't currently allowed by ``is_subscription_active``
        # — including a *missing* billing row — gets blocked. Lumping "no
        # row" with "legacy row" would let unfunded users chat through
        # cards 1+2 (whose LLM is on the user but whose container is on us).
        account = await billing_repo.get_by_owner_id(owner_id)
        if not is_subscription_active(account):
            return {
                "blocked": True,
                "code": "subscription_inactive",
                "message": ("Your subscription is not active. Reactivate to continue chatting."),
            }

        # --- Layer 2: card-3 credit balance --------------------------
        # provider_choice lives on the billing_accounts row (per owner);
        # credits also pool at the owner level so org members all draw
        # from one balance funded by the
        # admin. Keying on user_id meant org members saw balance=0 and
        # got blocked at chat with code=out_of_credits.
        if account is None or account.get("provider_choice") != "bedrock_claude":
            return {"blocked": False}

        balance = await credit_ledger.get_balance(owner_id, consistent=True)
        if balance <= 0:
            return {
                "blocked": True,
                "code": "out_of_credits",
                "message": "You're out of Claude credits. Top up to continue.",
            }
        return {"blocked": False}

    async def _create_connection(self, user_id: str, ip: str, token: str) -> GatewayConnection:
        """Create and connect a new GatewayConnection."""
        # Ensure the canonical set exists before passing it as a shared
        # reference. The GatewayConnection reads this set directly when
        # fanning out events — no copy, no sync, no race.
        if user_id not in self._frontend_connections:
            self._frontend_connections[user_id] = set()
        conn = GatewayConnection(
            user_id=user_id,
            ip=ip,
            token=token,
            management_api=self._management_api,
            frontend_connections=self._frontend_connections[user_id],
            conn_member_map=self._conn_member_map,
        )
        await conn.connect()
        self._connections[user_id] = conn
        return conn

    async def send_rpc(
        self,
        user_id: str,
        req_id: str,
        method: str,
        params: dict,
        ip: str,
        token: str,
    ) -> Any:
        """Send RPC via persistent connection (create / reopen as needed).

        Three repair paths:

        - **Parked** (WS torn down with active runs in flight): reopen the
          same connection, preserving `_active_runs` + `_pending_rpcs`.
          The reconnect floor below guards against storm cadence.
        - **Stale** (WS torn down with no active runs): close and recreate.
        - **Absent**: create fresh.

        The reconnect floor only gates the parked + stale repair paths,
        not first-time creates — a fresh user landing on /chat shouldn't
        wait 5s for the first connection.
        """
        async with self._lock:
            conn = self._connections.get(user_id)
            if conn is not None and conn._parked:
                self._respect_reconnect_floor(user_id)
                await conn.reopen()
            elif conn is not None and not conn.is_connected:
                self._respect_reconnect_floor(user_id)
                await conn.close()
                conn = None
            if conn is None:
                conn = await self._create_connection(user_id, ip, token)

        await conn.send_rpc(req_id, method, params)
        try:
            return await conn.wait_for_response(req_id)
        except Exception:
            put_metric("gateway.rpc.error", dimensions={"method": method})
            raise

    def _respect_reconnect_floor(self, user_id: str) -> None:
        """Raise if another reconnect for *user_id* fired within `_RECONNECT_FLOOR_S`.

        Keeps a misbehaving container from burning the open-handshake
        budget at sub-second cadence when the frontend's exponential
        reconnect collides with multiple in-flight retries server-side.
        Stamps the attempt time even when raising so the floor is
        sticky across the caller-retry boundary.
        """
        now = time.monotonic()
        last = self._last_reconnect_at.get(user_id, 0.0)
        if last and (now - last) < _RECONNECT_FLOOR_S:
            put_metric("gateway.connection", dimensions={"event": "reconnect_throttled"})
            raise RuntimeError(
                f"Gateway reconnect throttled for user {user_id} — "
                f"last attempt {now - last:.1f}s ago, floor {_RECONNECT_FLOOR_S}s"
            )
        self._last_reconnect_at[user_id] = now

    def add_frontend_connection(
        self,
        user_id: str,
        connection_id: str,
        member_id: str | None = None,
    ) -> None:
        """Register a frontend WS connection for event forwarding.

        *member_id* is the Clerk user_id of the org member who owns this
        connection. For personal (non-org) users it can be ``None`` — all
        events broadcast to all connections. For org members it enables
        per-member event routing so member A only sees their own chat
        streaming, not member B's.
        """
        if user_id not in self._frontend_connections:
            self._frontend_connections[user_id] = set()
        self._frontend_connections[user_id].add(connection_id)
        if member_id:
            self._conn_member_map[connection_id] = member_id

        # Cancel grace period if one is running
        grace = self._grace_tasks.pop(user_id, None)
        if grace and not grace.done():
            grace.cancel()

    def remove_frontend_connection(self, user_id: str, connection_id: str) -> None:
        """Unregister a frontend connection. Start grace period if none remain."""
        fcs = self._frontend_connections.get(user_id)
        if fcs:
            fcs.discard(connection_id)
        self._conn_member_map.pop(connection_id, None)

        # Start grace period if no frontend connections remain
        if not fcs and user_id in self._connections:
            self._grace_tasks[user_id] = asyncio.create_task(self._grace_close(user_id))

    async def _grace_close(self, user_id: str) -> None:
        """Wait grace period, then close gateway connection if still idle."""
        try:
            await asyncio.sleep(_GRACE_PERIOD)
            fcs = self._frontend_connections.get(user_id, set())
            if not fcs:
                await self.close_user(user_id)
        except asyncio.CancelledError:
            pass

    async def close_user(self, user_id: str) -> None:
        """Close gateway connection for a user."""
        conn = self._connections.pop(user_id, None)
        if conn:
            put_metric("gateway.connection", dimensions={"event": "disconnect"})
            await conn.close()
        self._frontend_connections.pop(user_id, None)
        self._grace_tasks.pop(user_id, None)
        # Drop the reconnect-floor entry so a fresh user (e.g. someone who
        # signed out and back in on the same backend task) doesn't inherit
        # a stale throttle from the previous session.
        self._last_reconnect_at.pop(user_id, None)

    async def broadcast_to_user(self, user_id: str, message: dict) -> None:
        """Send a message to all frontend connections for a user."""
        conn = self._connections.get(user_id)
        if conn:
            conn._forward_to_frontends(message)

    async def broadcast_to_member(self, owner_id: str, member_user_id: str, message: dict) -> None:
        """Send a message to frontend connections for a specific org member.

        For personal accounts (owner_id == member_user_id), this behaves
        identically to broadcast_to_user. For org accounts, it filters to
        only the connections belonging to *member_user_id*.
        """
        conn = self._connections.get(owner_id)
        if not conn:
            return
        # Personal account — no per-member filtering needed
        if owner_id == member_user_id:
            conn._forward_to_frontends(message)
            return
        # Org account — filter to this member's connections only
        gone: list[str] = []
        for conn_id in list(conn._frontend_connections):
            member = conn._conn_member_map.get(conn_id, "")
            if member.lower() != member_user_id.lower():
                continue
            try:
                if not self._management_api.send_message(conn_id, message):
                    gone.append(conn_id)
            except Exception:
                gone.append(conn_id)
        for conn_id in gone:
            conn._frontend_connections.discard(conn_id)

    async def close_all(self) -> None:
        """Shutdown: close all connections."""
        for user_id in list(self._connections.keys()):
            await self.close_user(user_id)

    async def emit_running_gauges(self) -> None:
        """Emit gateway-running-count gauges. Called by the lifespan loop.

        Replaces the old idle-reaper, which also computed these gauges as a
        side-effect. Post-flat-fee cutover there is no scale-to-zero, so the
        reaping loop is gone — but the running-count metric still drives the
        W5 alarm on gateway.connection.open.
        """
        from core.repositories import container_repo

        try:
            rows = await container_repo.get_by_status("running")
        except Exception:
            logger.exception("running_gauges: get_by_status failed")
            return

        parked_count = sum(1 for c in self._connections.values() if c._parked)
        try:
            gauge("gateway.running.count", len(rows))
            gauge("gateway.connection.open", len(self._connections))
            gauge("gateway.connection.parked", parked_count)
        except Exception:
            pass


def _parse_session_key(session_key: str) -> dict:
    """Parse an OpenClaw session key into its components.

    Shapes (from openclaw/src/routing/session-key.ts with dmScope=per-account-channel-peer):
      Personal webchat:  agent:<agentId>:main
      Org webchat:       agent:<agentId>:<clerk_user_id>
      Cron chat:         agent:<agentId>:cron:<cronId>
      Cron run event:    agent:<agentId>:cron:<cronId>:run:<runId>
      Channel DM:        agent:<agentId>:<channel>:<accountId>:direct:<peerId>
      Channel group:     agent:<agentId>:<channel>:group:<id>(:topic:<topicId>)?
      Channel room:      agent:<agentId>:<channel>:channel:<id>(:thread:<threadId>)?

    Returns dict with:
      - empty {} for malformed input
      - {agent_id, source} for webchat personal
      - {agent_id, source, member_id} for org webchat (member_id is the clerk user_id)
      - {agent_id, source, cron_id} for cron sessions (source="cron")
      - {agent_id, source, channel, peer_id} for channel DMs (source="dm")
      - {agent_id, source, channel, group_id} for channel groups (source="group")
      - {agent_id, source, channel, channel_id} for channel rooms (source="channel")
    """
    parts = session_key.split(":")
    if len(parts) < 3 or parts[0] != "agent":
        return {}
    agent_id = parts[1]

    # Cron sessions: agent:<agentId>:cron:<cronId>(:run:<runId>)?
    # Must check BEFORE webchat since 4-part cron keys would otherwise
    # fall through to channel parsing.
    if parts[2] == "cron":
        result = {
            "agent_id": agent_id,
            "source": "cron",
        }
        if len(parts) >= 4:
            result["cron_id"] = parts[3]
        return result

    # Webchat: 3 parts (agent:<agentId>:<sessionName>)
    if len(parts) == 3:
        if parts[2] == "main":
            return {"agent_id": agent_id, "source": "webchat"}
        # Org webchat — parts[2] is a Clerk user_id
        return {
            "agent_id": agent_id,
            "source": "webchat",
            "member_id": parts[2],
        }

    # Channel DM (per-account-channel-peer):
    # agent:<agentId>:<channel>:<accountId>:direct:<peerId>
    # In our design accountId == agentId so we don't extract parts[3] separately.
    if len(parts) == 6 and parts[4] == "direct":
        return {
            "agent_id": agent_id,
            "source": "dm",
            "channel": parts[2],
            "peer_id": parts[5],
        }

    # Channel group: agent:<agentId>:<channel>:group:<id>(:topic:<topicId>)?
    if len(parts) >= 5 and parts[3] == "group":
        return {
            "agent_id": agent_id,
            "source": "group",
            "channel": parts[2],
            "group_id": parts[4],
        }

    # Channel room: agent:<agentId>:<channel>:channel:<id>(:thread:<threadId>)?
    if len(parts) >= 5 and parts[3] == "channel":
        return {
            "agent_id": agent_id,
            "source": "channel",
            "channel": parts[2],
            "channel_id": parts[4],
        }

    return {"agent_id": agent_id, "source": "unknown"}
