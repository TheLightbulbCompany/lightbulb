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
from core.services.connection_service import ConnectionService

GATEWAY_PORT = 18789  # OpenClaw gateway port (avoid circular import with containers/)

logger = logging.getLogger(__name__)

# How long a connection caches its owner's recipient set (from the
# by-owner-id GSI) before re-querying. The recipient set is stable across a
# streaming run, so we query once per ~5s rather than once per chunk.
_RECIPIENTS_TTL_S = 5.0

_HANDSHAKE_TIMEOUT = 10  # seconds
_RPC_TIMEOUT = 30  # seconds
_RPC_DRAIN_TIMEOUT = 5.0  # graceful-shutdown: wait this long for in-flight RPCs to settle before closing the WS
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

# Bound on the per-connection side-run id set. Entries self-clean on the side
# run's terminal; this cap is only a safety net for a side run that never emits
# one. Far above any realistic count of concurrent `/btw` runs on one session.
_SIDE_RUN_IDS_CAP = 256


def _deliver_to_recipients(
    recipients: list[tuple[str, str, str]],
    message: dict,
    target_member_id: Optional[str],
    management_api: Any,
) -> tuple[int, int, list[str]]:
    """Send *message* to the chat connections among *recipients* (the
    ``(connectionId, member_userId, connectionType)`` rows from the by-owner-id
    GSI), returning ``(delivered, skipped, gone)``.

    - Filters to ``connectionType == "chat"`` so node/operator transparent pipes
      never receive chat frames.
    - Demuxes to *target_member_id* when set (case-insensitive — OpenClaw
      lowercases sessionKeys, so the parsed member id is lowercase while DDB
      stores the original Clerk user_id); broadcasts to every chat connection
      when it is ``None``.
    - ``gone`` holds ids whose ``send_message`` returned ``False`` (GoneException
      → safe to prune). A transient send *exception* is logged and skipped, never
      treated as gone — a blip must not delete a live socket's row.

    Pure (no I/O beyond the injected ``management_api``) so both the cached chat
    path (``GatewayConnection._forward_to_frontends``) and the connection-less
    pool broadcast (``GatewayConnectionPool._fanout_to_owner``) share one
    chat-filter/demux/send definition.
    """
    gone: list[str] = []
    delivered = 0
    skipped = 0
    for conn_id, member_id, conn_type in recipients:
        if conn_type != "chat":
            skipped += 1
            continue
        if target_member_id is not None and (member_id or "").lower() != target_member_id.lower():
            skipped += 1
            continue
        try:
            if not management_api.send_message(conn_id, message):
                gone.append(conn_id)
            else:
                delivered += 1
        except Exception:
            logger.warning("Transient forward failure to %s (not pruning)", conn_id)
    return delivered, skipped, gone


class GatewayConnection:
    """Single persistent WebSocket to a user's OpenClaw gateway."""

    def __init__(
        self,
        user_id: str,
        ip: str,
        token: str,
        management_api: Any,
        frontend_connections: Set[str],
        on_activity: Any = None,
        owner_id: Optional[str] = None,
        conn_svc: Optional[ConnectionService] = None,
    ) -> None:
        self.user_id = user_id
        # The owner key (org_id for org members, else user_id) this connection
        # is pooled under — the same key `GatewayConnectionPool._connections` is
        # keyed by. Explicit so the owner-keyed DDB fan-out can query
        # `query_by_owner(self.owner_id)` without overloading `user_id`. Defaults
        # to `user_id` for back-compat with direct test constructors.
        self.owner_id = owner_id or user_id
        self.ip = ip
        self.token = token
        self._management_api = management_api
        # Owner-keyed fan-out reads recipients from the ws-connections
        # by-owner-id GSI via this service — so any backend instance can deliver
        # to all of an owner's tabs/devices. Defaults to the module singleton.
        self._conn_svc = conn_svc or ConnectionService()
        # Short-TTL cache of the owner's recipient set so we don't query DDB
        # per streamed chunk. Invalidated on a dead-conn prune.
        self._recipients_cache: Optional[list[tuple[str, str, str]]] = None
        self._recipients_cache_ts: float = 0.0
        self._on_activity = on_activity  # callback(user_id) for idle tracking
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_rpcs: Dict[str, asyncio.Future] = {}
        # Shared reference to the pool's canonical set — NOT a copy. Backs only
        # the grace/idle-teardown check now (delivery + per-member demux are
        # sourced from the by-owner-id GSI, not from in-process state).
        self._frontend_connections = frontend_connections
        self._closed = False
        self._grace_task: Optional[asyncio.Task] = None
        self._billing_tasks: Set[asyncio.Task] = set()
        # Active LLM runs keyed by OpenClaw sessionKey (agent:{agentId}:{member}).
        # Populated by `mark_run_active` when we dispatch chat.send; cleared by
        # `_handle_message` when the matching chat-terminal event arrives.
        # Non-emptiness is the load-bearing signal for the parked-vs-dead
        # teardown decision in `_reader_loop` — see _on_reader_loop_exit.
        self._active_runs: Dict[str, Dict[str, Any]] = {}
        # Side-channel (`/btw`) run ids. A `/btw` aside runs CONCURRENTLY with a
        # normal turn but, on the wire, shares the same ``session_key`` (only the
        # runId differs). ``_active_runs`` is keyed by session_key — which is
        # load-bearing for the approval-pause mechanism (a same-session,
        # different-runId follow-up final must clear the window) — so it cannot
        # also be keyed by runId. Upstream sidesteps this entirely by tracking
        # runs per-runId and keeping btw ids in a separate set the primary
        # active-run state excludes (web UI ``chatSideResultTerminalRuns`` /
        # app-gateway.ts; TUI ``localBtwRunIds`` / tui-event-handlers.ts). We
        # mirror that: a side run's terminal must NOT clear the PRIMARY run's
        # session window, so we record side runIds here and the agent-event
        # handler skips all ``_active_runs`` mutation for them. Insertion-ordered
        # dict used as a bounded FIFO set; entries self-clean on their terminal.
        self._side_run_ids: Dict[str, None] = {}
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
        """Record an in-flight run for *session_key* (parked-vs-dead signal).

        Called from the chat dispatcher before the chat.send RPC is forwarded so
        the active-run window opens before the first inbound event. NOT a
        serialize gate — its sole consumer is the parked-vs-dead reconnect
        decision (`_on_reader_loop_exit` / `is_alive`), which keys on
        ``_active_runs`` non-emptiness. Idempotent — re-marking refreshes
        started_at without losing the existing last_event_at.
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
            # Set when OpenClaw emits an approval-requested agent event for this
            # session; consumed by `_is_true_terminal` so the interim chat
            # `final` that closes the approval bracket does NOT end the run.
            "awaiting_approval": False,
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

    def mark_side_run(self, run_id: str) -> None:
        """Record *run_id* as a side-channel (`/btw`) run.

        A side run shares the primary run's ``session_key`` on the wire, so the
        agent-event handler consults this set to AVOID mutating the primary
        run's ``_active_runs`` window when a side run's events / terminal
        arrive. Bounded FIFO; entries are removed on the side run's terminal
        (``_forget_side_run``), so the cap is only a safety net.
        """
        if not run_id:
            return
        self._side_run_ids[run_id] = None
        while len(self._side_run_ids) > _SIDE_RUN_IDS_CAP:
            oldest = next(iter(self._side_run_ids))
            del self._side_run_ids[oldest]

    def _is_side_run(self, run_id: str | None) -> bool:
        """Whether *run_id* is a tracked side-channel (`/btw`) run."""
        return bool(run_id) and run_id in self._side_run_ids

    def _forget_side_run(self, run_id: str) -> None:
        """Drop *run_id* from the side-run set (on its terminal)."""
        self._side_run_ids.pop(run_id, None)

    def _mark_awaiting_approval(self, session_key: str) -> None:
        """Flag the active run keyed by *session_key* as paused on an approval.

        When an exec tool needs an out-of-band (async) approval, OpenClaw's
        embedded agent emits an ``agent`` event with ``stream:"approval"``,
        ``data.phase:"requested"`` carrying the CURRENT run's id (upstream
        src/agents/embedded-agent-subscribe.handlers.tools.ts:1348-1384, on an
        exec result with status ``approval-pending``). That run (Run A) then
        reaches run-loop completion and emits its GENUINE terminal chat
        ``final`` — it does NOT pause-and-resume as the same run. The work
        continues only after the user decides, as a SEPARATE follow-up ``agent``
        run with a fresh runId (src/agents/bash-tools.exec-approval-followup.ts;
        upstream itself bounds that wait at 60s via waitForAgentFollowupRun).

        Recording the pause here lets `_is_true_terminal` suppress Run A's
        terminal ``final`` so the per-session run-active window stays open across
        the approval round-trip (no UI "dead air"). The window is keyed by
        ``session_key``, so it is the follow-up run's eventual ``final`` (same
        session, DIFFERENT runId) that re-clears it.

        No-op when no run is tracked for the key (cron / channel sessions).
        """
        run = self._active_runs.get(session_key)
        if run is not None:
            run["awaiting_approval"] = True

    def _is_true_terminal(self, session_key: str, state: str) -> bool:
        """True iff this chat-terminal event genuinely ends the per-session run.

        ``error`` and ``aborted`` are ALWAYS terminal — they clear the
        run-active window regardless of any pending approval, so a dead run can
        never be kept alive (see the regression tests covering this).

        A ``final`` is terminal UNLESS the session is paused awaiting an async
        exec approval. In that case the ``final`` is Run A's genuine terminal
        (it really did end — see `_mark_awaiting_approval`), but the work
        continues in a SEPARATE follow-up run, so clearing the window now would
        flash "dead air" until that follow-up starts. We consume the
        pending-approval flag (one suppressed ``final`` per approval pause-point,
        not a per-approval counter) and keep the window open; the follow-up
        run's later ``final`` — no approval outstanding — clears it. The window
        is keyed by ``session_key`` (not runId), which is why a DIFFERENT run's
        ``final`` legitimately clears it.

        Known gap (tracked follow-up): if the follow-up run never spawns — a
        denied approval with suppressed follow-up, or subagent/cron suppression
        (bash-tools.exec-approval-followup.ts) — no second ``final`` arrives and
        the window leaks until the connection-level parked→dead reaper tears the
        connection down. A bounded per-session safety-valve timeout (mirroring
        upstream's 60s follow-up wait) would close this; it is gated on first
        confirming the live container's approval mode.
        """
        if state in ("error", "aborted"):
            return True
        run = self._active_runs.get(session_key)
        if run is not None and run.get("awaiting_approval"):
            # Suppress Run A's genuine terminal final so the per-session window
            # survives the approval; the separate follow-up run's final clears it.
            run["awaiting_approval"] = False
            return False
        return True

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
        """Extract streaming text, thinking, tool, and activity events from an OpenClaw agent event.

        Agent events fire for every LLM token (no 150ms throttle).
        - ``stream: "assistant"`` events with text are forwarded as chunks.
        - ``stream: "reasoning"`` / ``"thinking"`` events are forwarded for real-time thinking.
        - ``stream: "tool"`` events are forwarded as tool_start/tool_end/tool_error,
          plus tool_progress for the mid-execution ``phase: "update"``.
        - ``stream: "command_output"`` events are forwarded as command_output
          (live stdout/stderr addressed to the matching tool pill).
        - ``stream: "compaction"`` (terminal ``end``) is forwarded as a compaction divider.
        - ``stream: "plan" | "patch" | "item" | "acp"`` are folded into one
          uniform ``agent_activity`` envelope for a single low-noise pill.

        ``approval`` and ``lifecycle`` are intentionally NOT handled here — they
        are consumed upstream in ``_handle_message`` (approval run-active window
        + lifecycle billing/end) and forwarding them would conflict with that
        state machine.

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
                elif phase == "update":
                    # Live tool progress: incremental output emitted mid-
                    # execution before the result phase. Upstream renders this
                    # via formatToolOutput(data.partialResult); we forward it so
                    # the running tool pill can show streaming output. Requires a
                    # toolCallId to address the live pill, so drop updates without
                    # one (there is nothing for the frontend to attach them to).
                    if tool_call_id:
                        transformed = {"type": "tool_progress", "tool": name, "toolCallId": tool_call_id}
                        if "partialResult" in data:
                            transformed["partialResult"] = data["partialResult"]

        elif stream == "command_output":
            # Streaming command stdout/stderr emitted mid-exec. Carries a
            # toolCallId so the frontend appends it to the matching live tool
            # pill. Without one there is nothing to address, so drop it.
            tool_call_id = data.get("toolCallId", "")
            if tool_call_id:
                transformed = {"type": "command_output", "toolCallId": tool_call_id}
                if "output" in data:
                    transformed["output"] = data["output"]

        elif stream == "compaction":
            # Context-compaction notice. Only the terminal 'end' renders the
            # divider; 'start' would leave a divider that never resolves.
            if data.get("phase") == "end":
                transformed = {"type": "compaction", "phase": "end"}

        elif stream in ("plan", "patch", "item", "acp"):
            # Low-noise "agent activity" surfaces upstream renders as rich
            # affordances (proposed plan, patch summary, item lifecycle, ACP
            # step). We fold them into one uniform envelope so the frontend
            # shows a single activity pill: `title` is the human label and
            # `detail` carries the full payload for the pill's expansion.
            # NOTE: ``approval`` and ``lifecycle`` are deliberately excluded —
            # they are consumed by _handle_message (approval run-active window
            # + lifecycle billing/end), so forwarding them here would conflict
            # with that state machine.
            transformed = {
                "type": "agent_activity",
                "activity": stream,
                "title": data.get("title", ""),
                "detail": data,
            }

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

    async def _forward_to_frontends(self, message: dict, target_member_id: str | None = None) -> None:
        """Send a message to the owner's frontend chat clients.

        Recipients are sourced from the ws-connections ``by-owner-id`` GSI (the
        single source of truth for fan-out, so any backend instance can reach
        all of an owner's tabs/devices) via a short-TTL cache, then:

        - filtered to ``connectionType == "chat"`` so node/operator transparent
          pipes never receive chat ``chunk``/``done`` events;
        - demuxed per member when *target_member_id* is set (case-insensitive,
          because OpenClaw lowercases sessionKeys so the member_id parsed from
          the event is lowercase while DDB stores the original Clerk user_id);
        - broadcast to every chat connection when *target_member_id* is ``None``
          (status changes, SWR revalidation, etc.).

        On a recipient-query failure we log and drop the event — there is no
        in-process fallback (DynamoDB is the single source of truth). On a
        send failure we prune the dead row (best-effort) so the GSI stops
        returning it.
        """
        recipients = await self._cached_owner_recipients()
        if recipients is None:
            # query failed; logged in the helper. No in-process fallback.
            return

        delivered, skipped, gone = _deliver_to_recipients(recipients, message, target_member_id, self._management_api)
        for conn_id in gone:
            await self._prune_dead_conn(conn_id)
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
                len(recipients),
            )

    async def _cached_owner_recipients(self) -> Optional[list[tuple[str, str, str]]]:
        """Return ``(connectionId, member_userId, connectionType)`` rows for
        ``self.owner_id`` from the by-owner-id GSI, cached on a short TTL.

        Returns ``None`` on query failure so the caller drops the event rather
        than falling back to the in-process set (DynamoDB is the single source of
        truth for fan-out recipients).

        The cache is busted on every ``$connect``/``$disconnect`` for this owner
        (``invalidate_recipients_cache`` from ``add``/``remove_frontend_connection``),
        so a newly-opened tab is picked up on its own first stream. The GSI is
        eventually consistent, but fan-out is always gated behind a ``chat.send``
        round-trip + LLM turn while the connection's row was written at
        ``$connect`` — so propagation has long since completed by the time any
        frame is delivered. The only residual is a brand-new tab whose *very
        first* action is a one-shot frame within the sub-second propagation
        window; accepted, negligible, and self-heals on the next event.
        """
        now = time.monotonic()
        if self._recipients_cache is not None and (now - self._recipients_cache_ts) < _RECIPIENTS_TTL_S:
            return self._recipients_cache
        try:
            recipients = await self._conn_svc.query_by_owner(self.owner_id)
        except Exception:
            logger.exception(
                "owner-keyed fan-out query failed for owner=%s — dropping event",
                self.owner_id,
            )
            return None
        self._recipients_cache = recipients
        self._recipients_cache_ts = now
        return recipients

    def invalidate_recipients_cache(self) -> None:
        """Drop the cached owner recipient set so the next fan-out re-queries
        the by-owner-id GSI. Called when a frontend connection is added/removed
        for this owner, so a newly-opened tab is included in its own first
        stream instead of waiting up to _RECIPIENTS_TTL_S behind a stale cache."""
        self._recipients_cache = None
        self._recipients_cache_ts = 0.0

    async def _prune_dead_conn(self, conn_id: str) -> None:
        """Best-effort prune of a dead connection row + recipient-cache bust."""
        try:
            # delete_connection is a blocking boto3 call; offload it so it doesn't
            # stall the event loop (and all concurrent chat streaming) during
            # fan-out. Mirrors query_by_owner's to_thread offload.
            await asyncio.to_thread(self._conn_svc.delete_connection, conn_id)
        except Exception:
            logger.warning("failed to prune dead conn %s", conn_id, exc_info=True)
        # Also drop it from the in-process set: that set now backs only the
        # grace/idle-teardown check (_grace_close), so leaving a pruned id there
        # could keep an idle gateway connection alive indefinitely.
        self._frontend_connections.discard(conn_id)
        # Invalidate so the next forward re-queries instead of re-sending to it.
        self._recipients_cache = None
        self._recipients_cache_ts = 0.0
        put_metric("gateway.frontend.prune")
        logger.info("Pruned gone frontend connection %s for owner %s", conn_id, self.owner_id)

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

    async def _handle_message(self, data: dict) -> None:
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
                # Approval pause: when a tool call needs an async approval,
                # OpenClaw emits this approval-requested agent event; the run
                # then reaches completion and emits its GENUINE terminal `final`,
                # with the work continuing as a SEPARATE follow-up run (see
                # `_mark_awaiting_approval` / `_is_true_terminal` for the full
                # sequence). Flag the run so `_is_true_terminal` suppresses that
                # terminal `final` and the per-session run-active window survives
                # the approval round-trip (otherwise → UI "dead air"). No-op for
                # cron/channel sessions, which never have a tracked active run.
                # Done before the channel/cron forwarding gate because the marker
                # is independent of whether we forward the event to the web UI.
                if (
                    stream == "approval"
                    and isinstance(payload.get("data"), dict)
                    and payload["data"].get("phase") == "requested"
                    and payload["data"].get("status") in ("pending", "unavailable")
                    and session_key
                    # A side (`/btw`) run's approval must not flag the PRIMARY
                    # run's window (shared session_key) as awaiting-approval —
                    # that would suppress the primary's real terminal.
                    and not self._is_side_run(payload.get("runId"))
                ):
                    self._mark_awaiting_approval(session_key)
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
                    await self._forward_to_frontends(transformed, target_member)

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
                run_id = payload["runId"]
                # Side-channel (`/btw`) terminal: a side run shares this
                # session_key with the primary run but owns NO active-run window
                # (the dispatcher skips mark_run_active for it). It must NOT
                # touch `_active_runs` — clearing here would tear down the
                # PRIMARY run's window mid-stream, dropping the busy-gate and
                # reintroducing the per-session JSONL write-lock race. It must
                # also NOT consult `_is_true_terminal` (that would consume the
                # primary run's awaiting_approval flag). So treat it as terminal
                # for ITS OWN run only — forget the id and forward `run_ended`
                # (the frontend drops it via its own side-run set). Mirrors
                # upstream's per-runId run tracking + the clients' separate
                # btw-id sets (app-gateway.ts chatSideResultTerminalRuns /
                # tui-event-handlers.ts isLocalBtwRunId).
                if self._is_side_run(run_id):
                    self._forget_side_run(run_id)
                    is_true_terminal = True
                else:
                    # Clear the active-run entry only on a TRUE terminal so
                    # subsequent reader_loop exits make the dead-vs-parked
                    # decision based on still-in-flight runs only. An interim
                    # `final` that merely closes a tool-approval bracket is NOT
                    # terminal (the run continues via a separate follow-up run),
                    # so `_is_true_terminal` keeps it active to avoid run-active
                    # "dead air" across the approval. `_is_true_terminal`
                    # consumes the pending-approval flag, so call it exactly once
                    # and reuse the result for both clearing the run and the
                    # authoritative `run_ended` forward below.
                    is_true_terminal = bool(session_key) and self._is_true_terminal(session_key, state)
                    if is_true_terminal:
                        self._clear_active_run(session_key)
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
                        await self._forward_to_frontends(fwd, target_member)
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
                        await self._forward_to_frontends(fwd, target_member)
                    fwd = {"type": "done"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    await self._forward_to_frontends(fwd, target_member)
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
                    await self._forward_to_frontends(fwd, target_member)
                elif state == "aborted":
                    put_metric("chat.error", dimensions={"reason": "aborted"})
                    fwd = {"type": "error", "message": "Agent run was cancelled"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    await self._forward_to_frontends(fwd, target_member)

                # Authoritative "run truly ended" frame. This is an Isol8-
                # synthesized frame — there is no upstream `run_ended` event; we
                # derive it from the chat terminal states. ADDITIONAL to the
                # done/error/aborted frames above — never a replacement. The
                # frontend keys off this (not a local heuristic) to drain its
                # queued messages for the run, so it must fire EXACTLY ONCE per
                # run and ONLY on a genuine termination. The `is_true_terminal`
                # gate guarantees we skip Run A's terminal `final` while it is
                # paused awaiting an async approval (the work resumes as a
                # separate follow-up run), so a paused-on-approval session never
                # drains early.
                if is_true_terminal:
                    run_ended: dict = {
                        "type": "run_ended",
                        "runId": run_id,
                        "outcome": "done" if state == "final" else state,
                    }
                    if event_agent_id:
                        run_ended["agent_id"] = event_agent_id
                    await self._forward_to_frontends(run_ended, target_member)

            else:
                # Forward other events as-is for SWR revalidation (broadcast to all)
                await self._forward_to_frontends(data)
            return

    async def _reader_loop(self) -> None:
        """Background task: read all messages from gateway WebSocket."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                    await self._handle_message(data)
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
        try:
            await self.connect()
        except Exception:
            # connect() failed — do NOT leave the connection parked with no
            # watchdog. That wedges it forever: every reopen re-dials and
            # fails, and the parked->dead escalation (which lets the pool
            # recreate from scratch) never fires. Re-arm so it can escalate.
            if self._active_runs and (self._parked_watchdog_task is None or self._parked_watchdog_task.done()):
                self._parked_watchdog_task = asyncio.create_task(self._parked_watchdog())
            raise
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
        """Shut down: drain in-flight RPCs, cancel reader, close WebSocket."""
        self._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection closed")
        self._closed = True
        # Wait for in-flight billing-resolver tasks so teardown doesn't drop usage events.
        if self._billing_tasks:
            try:
                await asyncio.wait(self._billing_tasks, timeout=5.0)
            except Exception:
                logger.exception(
                    "Error waiting for billing tasks during close for user %s",
                    self.user_id,
                )
        # Graceful RPC drain: the reader loop is still alive here, so in-flight
        # RPCs can still receive their `res` and resolve. Wait (bounded) for the
        # pending set to settle BEFORE tearing down the transport, instead of
        # rejecting them after WS close (which makes callers hang to _RPC_TIMEOUT).
        pending = [f for f in self._pending_rpcs.values() if not f.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=_RPC_DRAIN_TIMEOUT)
            except Exception:
                logger.exception(
                    "Error draining pending RPCs during close for user %s",
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
        # Reject any RPCs that did NOT settle during the drain window.
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
        conn_svc: Optional[ConnectionService] = None,
    ) -> None:
        self._management_api = management_api
        # Shared ConnectionService used by every GatewayConnection's owner-keyed
        # fan-out (query_by_owner on the by-owner-id GSI). Defaults to a fresh
        # instance — the same wiring as the module singleton elsewhere.
        self._conn_svc = conn_svc or ConnectionService()
        self._connections: Dict[str, GatewayConnection] = {}
        self._frontend_connections: Dict[str, Set[str]] = {}  # owner_id -> set of conn_ids
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
        per-user gateway connection. Silently no-ops when no connection exists
        for *user_id* yet — and NOTHING else populates ``_active_runs`` (inbound
        events only touch/clear an existing entry), so a mark issued before the
        connection exists is simply lost. The chat dispatcher therefore calls
        ``ensure_connection`` before marking on every non-side send, which
        guarantees a connection here. Idempotent.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.mark_run_active(session_key, agent_id)

    def clear_active_run(self, user_id: str, session_key: str) -> None:
        """Tear down the active-run window opened by ``mark_run_active``.

        The dispatcher marks a run active *before* issuing chat.send so the
        parked-vs-dead window is open for the whole dispatch. If that chat.send
        never reaches OpenClaw (timeout, container unreachable, gateway error),
        no terminal event will ever arrive to clear the window — leaving a stale
        entry that keeps the connection parking on a WS drop forever. The
        dispatcher calls this on the chat.send failure path. Silently no-ops
        when no connection exists. Idempotent.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn._clear_active_run(session_key)

    def mark_side_run(self, user_id: str, run_id: str) -> None:
        """Record *run_id* as a side-channel (`/btw`) run on *user_id*'s
        connection so its terminal never clears the PRIMARY run's session
        window (both share the session_key). The dispatcher calls this after
        ``ensure_connection`` for a side send, so a connection exists here.
        Silently no-ops when none does.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.mark_side_run(run_id)

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
            # `user_id` here IS the pool's owner key (org_id or user_id);
            # pass it explicitly as owner_id so the fan-out is unambiguous.
            owner_id=user_id,
            conn_svc=self._conn_svc,
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
        conn = await self.ensure_connection(user_id, ip, token)
        await conn.send_rpc(req_id, method, params)
        try:
            return await conn.wait_for_response(req_id)
        except Exception:
            put_metric("gateway.rpc.error", dimensions={"method": method})
            raise

    async def ensure_connection(self, user_id: str, ip: str, token: str) -> GatewayConnection:
        """Get-or-create the per-user gateway connection and return it.

        Same three repair paths as ``send_rpc`` (parked→reopen, stale→recreate,
        absent→create), adopting the freshly-resolved endpoint. Extracted so the
        chat dispatcher can GUARANTEE the connection exists before it records the
        active-run window: ``mark_run_active`` silently no-ops when no connection
        exists yet, and on a fresh backend task it is ``send_rpc`` that would
        otherwise create the connection — so marking before the first send would
        be lost. ``send_rpc`` itself routes through here, so the dispatcher's
        pre-send call is idempotent (the second resolution finds the live
        connection on the fast path).
        """
        async with self._lock:
            conn = self._connections.get(user_id)
            # Always adopt the freshly-resolved endpoint. Container task IPs are
            # ephemeral (every ECS task replacement changes them); a parked/stale
            # connection still holds the previous task's IP, so reopen() would
            # otherwise re-dial a dead address forever.
            if conn is not None and conn.ip != ip:
                logger.info(
                    "Gateway endpoint changed for user=%s %s -> %s — adopting before reconnect",
                    user_id,
                    conn.ip,
                    ip,
                )
                conn.ip = ip
            if conn is not None and conn._parked:
                self._respect_reconnect_floor(user_id)
                await conn.reopen()
            elif conn is not None and not conn.is_connected:
                self._respect_reconnect_floor(user_id)
                await conn.close()
                conn = None
            if conn is None:
                conn = await self._create_connection(user_id, ip, token)
            return conn

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

    def add_frontend_connection(self, user_id: str, connection_id: str) -> None:
        """Register a frontend WS connection so its owner's recipient cache is
        busted (it's picked up on the next fan-out) and any pending idle-teardown
        grace period is cancelled.

        Per-member routing is NOT tracked here: delivery + demux are sourced from
        the by-owner-id GSI (the row's ``userId`` is the member), so the only
        in-process role of a registered connection is the grace/idle-teardown
        signal below.
        """
        if user_id not in self._frontend_connections:
            self._frontend_connections[user_id] = set()
        self._frontend_connections[user_id].add(connection_id)

        # Bust the owner's recipient cache so a just-opened tab is picked up on
        # its own first stream instead of waiting behind a stale cached set for
        # up to _RECIPIENTS_TTL_S. (The by-owner-id GSI is the source; this only
        # forces a re-query on the next fan-out for this owner.)
        conn = self._connections.get(user_id)
        if conn is not None:
            conn.invalidate_recipients_cache()

        # Cancel grace period if one is running
        grace = self._grace_tasks.pop(user_id, None)
        if grace and not grace.done():
            grace.cancel()

    def remove_frontend_connection(self, user_id: str, connection_id: str) -> None:
        """Unregister a frontend connection. Start grace period if none remain."""
        fcs = self._frontend_connections.get(user_id)
        if fcs:
            fcs.discard(connection_id)

        # Bust the recipient cache so a removed tab stops receiving events
        # promptly rather than lingering in a cached set for up to the TTL.
        conn = self._connections.get(user_id)
        if conn is not None:
            conn.invalidate_recipients_cache()

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
            await conn._forward_to_frontends(message)

    async def broadcast_to_member(self, owner_id: str, member_user_id: str, message: dict) -> None:
        """Send a message to the owner's chat tabs, demuxed to one org member.

        For personal accounts (``owner_id == member_user_id``) it broadcasts to
        all of the owner's chat connections; for org accounts it demuxes to
        *member_user_id*'s connections only.

        Routes through the pool-level owner-keyed fan-out (``query_by_owner`` on
        the by-owner-id GSI), NOT through a ``GatewayConnection``. The node-status
        path that calls this (``node_proxy.handle_node_connect``/disconnect)
        creates a ``NodeUpstreamConnection``, not a chat ``GatewayConnection`` —
        so requiring one here meant a status ping landing on a task with no
        active chat for the owner silently dropped (and at N>1 it would miss the
        member's tab on another task entirely). The pool path needs no
        per-owner ``GatewayConnection`` to exist.
        """
        target = None if owner_id == member_user_id else member_user_id
        await self._fanout_to_owner(owner_id, message, target_member_id=target)

    async def _fanout_to_owner(self, owner_id: str, message: dict, target_member_id: Optional[str] = None) -> None:
        """Owner-keyed fan-out reachable WITHOUT a ``GatewayConnection``.

        Queries the by-owner-id GSI directly via the pool's ``ConnectionService``
        and delivers through the shared chat-filter/demux helper, so any backend
        instance can push to an owner's tabs even when no chat gateway connection
        exists on this task. On a query failure we log and drop (DynamoDB is the
        single source of truth — no in-process fallback).
        """
        try:
            recipients = await self._conn_svc.query_by_owner(owner_id)
        except Exception:
            logger.exception(
                "owner fan-out query failed for owner=%s — dropping broadcast",
                owner_id,
            )
            return
        _delivered, _skipped, gone = _deliver_to_recipients(recipients, message, target_member_id, self._management_api)
        for conn_id in gone:
            await self._prune_owner_connection(owner_id, conn_id)

    async def _prune_owner_connection(self, owner_id: str, conn_id: str) -> None:
        """Best-effort prune of a gone connection seen during a pool-level
        broadcast: delete the ws-connections row and, if a ``GatewayConnection``
        exists for this owner on this task, drop the id from its in-process set +
        bust its recipient cache so a subsequent chat stream re-queries."""
        try:
            await asyncio.to_thread(self._conn_svc.delete_connection, conn_id)
        except Exception:
            logger.warning("failed to prune dead conn %s", conn_id, exc_info=True)
        fcs = self._frontend_connections.get(owner_id)
        if fcs:
            fcs.discard(conn_id)
        conn = self._connections.get(owner_id)
        if conn is not None:
            conn.invalidate_recipients_cache()
        # Observability parity with GatewayConnection._prune_dead_conn so node-path
        # prunes are visible in CloudWatch too.
        put_metric("gateway.frontend.prune")
        logger.info("Pruned gone frontend connection %s for owner %s", conn_id, owner_id)

    async def close_all(self) -> None:
        """Shutdown: close all connections concurrently."""
        await asyncio.gather(
            *(self.close_user(uid) for uid in list(self._connections.keys())),
            return_exceptions=True,
        )

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
