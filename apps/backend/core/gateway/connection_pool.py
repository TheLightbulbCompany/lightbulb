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
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from websockets import connect as ws_connect

from core.gateway.frame_sender import FrameSender
from core.gateway.operator_rpc import GATEWAY_PORT, GatewayConnectAuthError, perform_signed_connect
from core.observability.logging import bind_log_context
from core.observability.metrics import put_metric, gauge
from core.repositories import channel_link_repo
from core.services.connection_service import ConnectionService

logger = logging.getLogger(__name__)

# How long a connection caches its owner's recipient set (from the
# by-owner-id GSI) before re-querying. The recipient set is stable across a
# streaming run, so we query once per ~5s rather than once per chunk.
_RECIPIENTS_TTL_S = 5.0

_HANDSHAKE_TIMEOUT = 10  # seconds
_RPC_TIMEOUT = 30  # seconds
_RPC_DRAIN_TIMEOUT = 5.0  # graceful-shutdown: wait this long for in-flight RPCs to settle before closing the WS
# Bound on the paid-owner `is_owner_blocked` read inside `_budget_error_code`.
# That call is only a cheap fast-gate row read while the row is fresh; once it
# goes stale (METRONOME_GATE_STALENESS_S, default 60s — true for any run over
# a minute) it synchronously does a live Metronome round trip + a DDB write,
# and possibly a second Metronome round trip + more writes. `_handle_message`
# awaits `_budget_error_code` before forwarding the error frame and the
# synthesized `run_ended` frame, and `_reader_loop` awaits `_handle_message`
# serially — so without a bound here, a slow/degraded Metronome stalls
# delivery of every subsequent event on this owner's gateway connection.
_BUDGET_RECLASSIFY_TIMEOUT_S = 2.0  # seconds
_GRACE_PERIOD = 30  # seconds before closing idle connection
_PUSH_GRACE_CEILING_S = 300  # max extra hold while a run is active (bounded by parked watchdog)
_ACTIVE_RUN_POLL_S = 5  # re-check cadence for the active-run hold

# Second, longer grace applied ONLY to free-tier (trial_local) owners after
# the gateway WS is closed: if the owner still looks idle by every liveness
# check after this window, the container itself is stopped (desiredCount=0).
# Sub-spec 3 (event-driven scale-to-zero). Deliberately generous — a trial
# container costs ~$0.02/hr, but killing a container out from under a slow
# turn or a just-reopened tab costs trust. Over-preserve, never over-kill.
_TRIAL_STOP_GRACE_S = 150

# Layer 3 of scale-to-zero: recovers a LOST API Gateway `$disconnect`. The
# browser pings every 30s (see websocket_chat.py's ping handler); if API
# Gateway fails to deliver a `$disconnect` for a closed tab, nothing else
# ever calls `remove_frontend_connection` for it, so the owner's
# `_frontend_connections` entry never empties, `_grace_close` never arms,
# and a trial container idles forever even with nobody home. 180s = 6
# missed pings is comfortably past any single dropped ping (jitter, a
# backgrounded tab throttled by the browser) while still catching a truly
# gone tab in a bounded window. On expiry we force-clear the stale entry
# and flow into the exact same `remove_frontend_connection` tail a normal
# disconnect takes — so Task 12's veto ladder (`_maybe_stop_trial_container`,
# gated on cross-task DDB truth via `count_live_chat_connections`) still
# owns the actual stop decision. A healthy tab on another backend task is
# still protected.
_CLIENT_DEADLINE_S = 180

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

# Auth-failure backoff, per user, alongside the reconnect floor. The gateway
# locks a client IP+credential scope out for 5 minutes after 10 failed auth
# attempts in 60s (AUTH_RATE_LIMITED); the 5s floor alone allows 12
# attempts/min, so retrying a stale token at floor cadence trips the lockout
# and bricks the owner's connectivity. Exponential per-user hold
# (base × 2^(n-1), capped), with the gateway's own retryAfterMs dominant
# when longer. Cleared by the next successful connect.
_AUTH_BACKOFF_BASE_S = 60
_AUTH_BACKOFF_CAP_S = 300

# How stale `last_event_at` on an active run must be before the parked
# connection counts as "agent appears stuck" for observability. Set by spec
# §6 — we don't ESCALATE on this in this PR (deferred follow-up); we only
# emit `gateway.run.long_idle` metrics + structured logs so the deadlock-
# vs-slow-turn signal is greppable in CloudWatch Insights.
_RUN_LONG_IDLE_S = 300

# Cadence at which a live GatewayConnection re-stamps its DDB gateway-liveness
# lease (a serialized full ``mark_gateway_connected`` upsert via
# ``_write_liveness``) so the row stays fresh -- and is recreated if the initial
# connect put was lost -- for as long as the connection is alive: active,
# idle-open, OR parked (a
# parked connection keeps is_alive True while it presumes the container alive).
# Must be well under connection_service._LIVENESS_STALE_SECONDS (180s) so the
# read side never flaps a healthy connection to "not connected", and well under
# the row's ttl window so DDB's TTL sweep never deletes a live marker.
_LIVENESS_REFRESH_SECONDS = 60

# TTL for the per-connection agents.list name cache backing push-notification
# titles. Names change rarely; a short TTL keeps run-completion pushes off a
# fresh RPC per reply without letting a rename stay stale for long.
_AGENT_NAME_TTL_S = 60.0

# Bound on the per-connection side-run id set. Entries self-clean on the side
# run's terminal; this cap is only a safety net for a side run that never emits
# one. Far above any realistic count of concurrent `/btw` runs on one session.
_SIDE_RUN_IDS_CAP = 256

# A chat run whose terminal arrives later than this is a hung/stale outlier
# (e.g. a terminal re-delivered after a reconnect), NOT a real latency
# datapoint — emitting its elapsed would inflate the chat.e2e.latency p99 and
# can false-fire W9 (p99 > 20s SLO). 10 min is far past any legitimate chat
# round-trip; over-ceiling terminals bump chat.run.abandoned instead.
_E2E_LATENCY_CEILING_MS = 10 * 60 * 1000  # 10 minutes


def _deliver_to_recipients(
    recipients: list[tuple[str, str, str]],
    message: dict,
    target_member_id: Optional[str],
    sender: "FrameSender",
    on_gone: Optional[Any] = None,
) -> tuple[int, int]:
    """Enqueue *message* to the chat connections among *recipients* (the
    ``(connectionId, member_userId, connectionType)`` rows from the by-owner-id
    GSI), returning ``(enqueued, skipped)``.

    - Filters to ``connectionType == "chat"`` so node/operator transparent pipes
      never receive chat frames.
    - Demuxes to *target_member_id* when set (case-insensitive — OpenClaw
      lowercases sessionKeys, so the parsed member id is lowercase while DDB
      stores the original Clerk user_id); broadcasts to every chat connection
      when it is ``None``.

    Delivery is asynchronous: the ``FrameSender`` drains each connection's
    queue off the event loop, preserving per-connection order. A recipient
    that turns out to be Gone is pruned via *on_gone* (an async callable
    taking the connection id); transient failures are logged and skipped by
    the sender, never treated as gone — a blip must not delete a live
    socket's row.

    Shared by the cached chat path (``GatewayConnection._forward_to_frontends``)
    and the connection-less pool broadcast
    (``GatewayConnectionPool._fanout_to_owner``) so both keep one
    chat-filter/demux definition.
    """
    enqueued = 0
    skipped = 0
    for conn_id, member_id, conn_type in recipients:
        if conn_type != "chat":
            skipped += 1
            continue
        if target_member_id is not None and (member_id or "").lower() != target_member_id.lower():
            skipped += 1
            continue
        sender.enqueue(conn_id, message, on_gone=on_gone)
        enqueued += 1
    return enqueued, skipped


# The user-facing text for each way an owner can run out of Isol8 budget.
# ONE copy, shared by gate_chat's pre-send refusal and the gateway's mid-run
# reclassification: a turn refused before it starts and a turn refused halfway
# through are the same event to the user and must never drift into two wordings.
# The web card renders from `code` alone (see ChatErrorCard); these strings are
# what non-web surfaces — Paperclip run rows, logs, the wake refusal frame — show.
BUDGET_BLOCK_MESSAGES = {
    "trial_expired": "Your 3-day free trial has ended. Pick a plan to keep your agent.",
    "trial_daily_cap": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    "out_of_credits": "You're out of Claude credits. Top up to continue.",
    "subscription_inactive": "Your subscription is not active. Reactivate to continue chatting.",
}


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
        sender: Optional[FrameSender] = None,
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
        # Ordered non-blocking frame delivery. The pool passes its shared
        # instance so append-delta continuity state is per-connectionId across
        # every GatewayConnection; the default covers direct constructions
        # (tests) only.
        self._sender = sender or FrameSender(management_api)
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
        # Fire-and-forget run-completion push tasks (mirrors _billing_tasks):
        # tracked so close() can drain them instead of cancelling a send.
        self._push_tasks: Set[asyncio.Task] = set()
        # One-shot completion observers keyed by runId (== chat.send's
        # idempotencyKey, which OpenClaw echoes as `runId` on every event for
        # the run — chat.ts:2450). Registered via
        # ``register_run_completion_callback`` by a caller that needs to
        # observe a SPECIFIC dispatched run's terminal (final/error/aborted)
        # without being the browser tab that sent it — e.g. the server-owned
        # bootstrap dispatcher (INV-5, spec PR-6). Consumed (popped) exactly
        # once, from the same TRUE-terminal gate ``_dispatch_push_notification``
        # uses, then dropped — never re-fires for a re-delivered/duplicate
        # terminal.
        self._run_completion_callbacks: Dict[str, Callable[[str, dict], Awaitable[None]]] = {}
        self._run_completion_tasks: Set[asyncio.Task] = set()
        # agent_id -> display name, TTL-cached from agents.list for push titles.
        # None until first resolve; _agent_names_at is the monotonic stamp.
        self._agent_names: Optional[Dict[str, str]] = None
        self._agent_names_at: float = 0.0
        # Active LLM runs keyed by OpenClaw sessionKey (agent:{agentId}:{member}).
        # Populated by `mark_run_active` when we dispatch chat.send; cleared by
        # `_handle_message` when the matching chat-terminal event arrives.
        # Non-emptiness is the load-bearing signal for the parked-vs-dead
        # teardown decision in `_reader_loop` — see _on_reader_loop_exit.
        self._active_runs: Dict[str, Dict[str, Any]] = {}
        # Last-forwarded run-active value per webchat session_key. Deduplicates
        # `sessions.changed` bursts so only transitions produce a `run_active`
        # frame. SEPARATE from `_active_runs` (which is the parked-vs-dead
        # signal) — never conflate the two.
        self._run_state: dict[str, bool] = {}
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
        # Periodic lease-refresh for the DDB gateway-liveness row: runs while
        # this connection is alive so the row's ttl never lapses under a
        # long-lived connection (and the read side can tell fresh from
        # expired-but-unswept). Started/cancelled in _note_gateway_liveness.
        self._liveness_refresh_task: Optional[asyncio.Task] = None
        # Serializes every gateway-liveness write for this connection so their
        # ORDER matches the true is_alive transition order -- independent
        # create_tasks don't preserve completion order, so a connect put racing
        # a disconnect delete could otherwise land delete-first and then the
        # put recreates a dead marker (false "ready" for the freshness window).
        self._liveness_lock = asyncio.Lock()
        # Terminal marker: once a disconnect is noted, no put (initial or a
        # refresh tick already in flight) may recreate the liveness row. Cleared
        # on (re)connect. Checked INSIDE the lock so there is no TOCTOU with a
        # concurrently-enqueued delete.
        self._liveness_dead = False
        # Per-instance identity stamped on this connection's gateway-liveness
        # row. Makes the terminal delete owner-safe ACROSS connections: a
        # replacement connection (stale-conn swap, rolling-deploy overlap) writes
        # its own token, so this connection's delete -- conditioned on the token
        # -- can't remove the replacement's live marker. The per-connection
        # _liveness_lock only orders writes WITHIN one connection; the token is
        # what orders them across connection replacement (and across instances).
        self._liveness_token = uuid.uuid4().hex

    def _note_gateway_liveness(self, *, connected: bool) -> None:
        """Record the durable (cross-instance) DDB liveness transition (readiness
        store R4's replacement -- see ``core.services.connection_service.
        mark_gateway_connected`` / ``mark_gateway_disconnected`` and
        ``core.services.readiness_state``).

        The actual DDB write is dispatched through ``_write_liveness`` (never
        awaited inline: this must not add a round-trip to the connect/close/park
        hot path) and SERIALIZED behind ``_liveness_lock`` so a connect put and
        a disconnect delete can't complete out of order. The ``_liveness_dead``
        terminal flag is flipped synchronously here (before the delete task is
        even created) so any put still in flight no-ops instead of resurrecting
        a dead marker.

        Also owns the lifecycle of the periodic lease-refresh loop: connect
        (and reopen, which re-enters connect) starts it; every disconnect /
        park-escalation / close stops it -- so the lease is refreshed for
        exactly as long as ``is_alive`` holds. NB: the refresh loop is NOT
        cancelled here on disconnect. Cancelling a task caught inside
        ``refresh_gateway_liveness`` raises ``CancelledError`` at the ``await``
        but does NOT stop the boto3 write already running in the to_thread
        pool; that write would then land AFTER the terminal delete and, via the
        refresh's ``attribute_not_exists`` recovery path, resurrect a dead
        marker (read as ``ready`` for the freshness window). Cancellation is
        therefore deferred to ``_write_liveness``'s delete branch, which runs
        only after the delete has serialized behind any in-flight refresh
        under ``_liveness_lock`` -- catching the refresh loop in its sleep,
        never mid-write. ``_liveness_dead`` (flipped synchronously here) stops
        the loop from issuing any further write in the meantime.
        """
        if connected:
            self._liveness_dead = False
            self._start_liveness_refresh()
        else:
            self._liveness_dead = True
        asyncio.create_task(self._write_liveness(connected=connected))

    async def _write_liveness(self, *, connected: bool) -> None:
        """Serialized DDB liveness write. Puts are gated on ``_liveness_dead``
        (checked INSIDE the lock) so a put that lost the race to a disconnect
        can't recreate the row; the delete always applies (it is the terminal
        transition). Errors are swallowed inside the service methods."""
        async with self._liveness_lock:
            if connected:
                if self._liveness_dead:
                    return
                await self._conn_svc.mark_gateway_connected(self.owner_id, conn_token=self._liveness_token)
            else:
                await self._conn_svc.mark_gateway_disconnected(self.owner_id, conn_token=self._liveness_token)
        if not connected:
            # Terminal delete has now landed on the server STRICTLY AFTER any
            # in-flight refresh (the refresh holds _liveness_lock across its
            # boto3 write, so acquiring the lock above waited for that write to
            # finish). Only now is it safe to stop the refresh loop: it is
            # sleeping between ticks, so cancellation can't interrupt a write.
            # Deferring the cancel to here (instead of _note_gateway_liveness)
            # is what closes the resurrect-after-delete race.
            self._cancel_liveness_refresh()
        # A liveness flip moves a fact compute_status composes (runtime_phase /
        # chat_available read is_gateway_connected), yet the DDB write above is
        # NOT one of the repo write sites publish_status_changed rides. Without
        # this nudge a ready tab whose /container/status poller has backed off
        # only learns of a gateway death/recovery on a slow backstop or focus
        # revalidation. Fired OUTSIDE the lock (best-effort, never awaited into
        # the write) so it can't extend the serialized hot path. INV-1: nudge
        # only -- compute_status stays the sole composer.
        from core.services.status_events import publish_status_changed

        publish_status_changed(self.owner_id)

    def _start_liveness_refresh(self) -> None:
        """Start the periodic gateway-liveness lease refresh (idempotent)."""
        if self._liveness_refresh_task is not None and not self._liveness_refresh_task.done():
            return
        self._liveness_refresh_task = asyncio.create_task(self._liveness_refresh_loop())

    def _cancel_liveness_refresh(self) -> None:
        if self._liveness_refresh_task is not None and not self._liveness_refresh_task.done():
            self._liveness_refresh_task.cancel()
        self._liveness_refresh_task = None

    async def _liveness_refresh_loop(self) -> None:
        """Re-stamp the DDB gateway-liveness lease every
        ``_LIVENESS_REFRESH_SECONDS`` while this connection is alive.

        Each tick calls ``refresh_gateway_liveness`` (a CONDITIONAL upsert:
        absent OR still owned by this connection's token). Unlike the
        unconditional ``mark_gateway_connected`` claim, it RECREATES a lost row
        (missed-connect recovery) yet cannot steal ownership back from a
        replacement connection that already claimed the row with a newer token
        (rolling-deploy overlap). Serialized under ``_liveness_lock`` and gated
        on ``_liveness_dead`` (both inside the lock) so a tick racing a
        disconnect can neither resurrect nor re-own the row. Errors are
        swallowed inside the service call; this loop only exits on cancellation.
        """
        try:
            while True:
                await asyncio.sleep(_LIVENESS_REFRESH_SECONDS)
                async with self._liveness_lock:
                    if self.is_alive and not self._liveness_dead:
                        await self._conn_svc.refresh_gateway_liveness(self.owner_id, conn_token=self._liveness_token)
        except asyncio.CancelledError:
            raise

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

        async def _on_gone(conn_id: str) -> None:
            self._frontend_connections.discard(conn_id)
            put_metric("gateway.frontend.prune")
            logger.info("Pruned gone frontend connection %s for user %s", conn_id, self.user_id)

        for conn_id in list(self._frontend_connections):
            self._sender.enqueue(conn_id, message, on_gone=_on_gone)

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

    def mark_run_active(self, session_key: str, agent_id: str | None = None, member_id: str | None = None) -> None:
        """Record an in-flight run for *session_key* (parked-vs-dead signal).

        Called from the chat dispatcher before the chat.send RPC is forwarded so
        the active-run window opens before the first inbound event. NOT a
        serialize gate — its sole consumer is the parked-vs-dead reconnect
        decision (`_on_reader_loop_exit` / `is_alive`), which keys on
        ``_active_runs`` non-emptiness. Idempotent — re-marking refreshes
        started_at without losing the existing last_event_at.

        *member_id* is the CANONICAL Clerk id of the dispatching member (the JWT
        sub, mixed-case) — carried on the entry so the run-completion push can
        target the users-table PK. It must NOT come from the echoed session key,
        which OpenClaw lowercases (a case-sensitive PK lookup would miss).

        The dict key is folded to lowercase because the dispatcher stores the
        CANONICAL (mixed-case) key while OpenClaw echoes it LOWERCASED; only the
        userId segment can carry case (agentId is a lowercase UUID, sessionId is
        regex-forced lowercase), so ``key.lower()`` on both sides always agrees.
        """
        key = session_key.lower()
        now = time.monotonic()
        existing = self._active_runs.get(key)
        if existing is not None:
            existing["started_at"] = now
            existing["last_event_at"] = now
            if agent_id:
                existing["agent_id"] = agent_id
            if member_id:
                existing["member_id"] = member_id
            return
        self._active_runs[key] = {
            "started_at": now,
            "last_event_at": now,
            "agent_id": agent_id or "",
            # Canonical dispatch member id for the run-completion push audience.
            "member_id": member_id,
            # Set when OpenClaw emits an approval-requested agent event for this
            # session; consumed by `_is_true_terminal` so the interim chat
            # `final` that closes the approval bracket does NOT end the run.
            "awaiting_approval": False,
        }

    def _touch_run(self, session_key: str) -> None:
        """Bump ``last_event_at`` on the active run keyed by *session_key*.

        No-op if no run is active for the key (e.g. cron / channel sessions).
        Cheap enough to call on every inbound agent/chat event.

        Normalized to lowercase — see `mark_run_active` for why store/read
        must agree on casing.
        """
        # Fold the key: the store side (mark_run_active) lowercases, so reads
        # keyed on the echoed (already-lowercased) key must too.
        run = self._active_runs.get(session_key.lower())
        if run is not None:
            run["last_event_at"] = time.monotonic()

    def reset_run_state_dedup(self) -> None:
        """Forget the last-forwarded ``run_active`` per session so the next
        ``sessions.changed`` re-forwards it as a fresh transition.

        Called when a frontend (re)connects: the dedup in the ``sessions.changed``
        handler suppresses repeat ``hasActiveRun:true`` frames, but a tab that
        opens/reconnects mid-run has no local busy state (the frontend clears it
        on disconnect), so it needs the ongoing run re-announced. Clearing the
        map makes the next event a transition again. Separate from
        ``_active_runs`` (untouched — that is the parked-vs-dead signal).
        """
        self._run_state.clear()

    def _clear_active_run(self, session_key: str) -> None:
        """Pop the run keyed by *session_key* (chat.final / error / aborted)."""
        self._active_runs.pop(session_key.lower(), None)
        # Reset run-active dedup so the next run's first `hasActiveRun:true`
        # sessions.changed is forwarded as a fresh transition. (The frontend
        # busy-gate is already closed by the run_ended/done/error frame; this
        # only clears our transition bookkeeping.)
        self._run_state.pop(session_key, None)

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

        Normalized to lowercase — see `mark_run_active` for why store/read
        must agree on casing.
        """
        run = self._active_runs.get(session_key.lower())
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
        run = self._active_runs.get(session_key.lower())
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
        await self._subscribe_session_events()
        put_metric("gateway.connection", dimensions={"event": "connect"})
        self._emit_status_change("HEALTHY", "Gateway connected")
        # Durable liveness signal (R4 kill) -- covers both a fresh connect()
        # and a reopen() (which calls connect() internally).
        self._note_gateway_liveness(connected=True)
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

        The signed handshake itself lives in `operator_rpc.perform_signed_connect`
        (shared with the one-shot operator calls). The pool opts into
        `tool-events` so OpenClaw streams `stream:"tool"` events to us — without
        that capability the gateway silently drops them.
        """
        await perform_signed_connect(
            self._ws,
            owner_id=self.user_id,
            token=self.token,
            caps=["tool-events"],
            recv_timeout=_HANDSHAKE_TIMEOUT,
        )

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

    async def _subscribe_session_events(self) -> None:
        """Subscribe this connection to OpenClaw `sessions.changed` broadcasts.

        OpenClaw only broadcasts session events to connections that have called
        `sessions.subscribe` (fork src/gateway/server-methods/sessions.ts:906),
        and drops the subscription on disconnect. Sent on every connect; because
        `reopen()` calls `connect()`, this re-subscribes on reconnect too.
        Fire-and-forget: the `{subscribed:true}` ack is not needed to proceed —
        if it never lands we simply fall back to stream-derived busy state, so
        we don't block connect on it. The ack `res` is harmlessly ignored by
        `_handle_message` (no matching entry in `_pending_rpcs`).
        """
        req_id = str(uuid.uuid4())
        frame = {"type": "req", "id": req_id, "method": "sessions.subscribe", "params": {}}
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:
            logger.warning("sessions.subscribe send failed user=%s", self.user_id, exc_info=True)

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

    async def _budget_error_code(self) -> tuple[str, str] | None:
        """Is this owner's `rate_limit` actually an Isol8 budget refusal?

        The bedrock-gate returns budget denials as `429 ThrottlingException`
        so the container never learns it's behind a proxy. OpenClaw classifies
        that as `errorKind: "rate_limit"` and — at two or more model attempts —
        discards the gate's own message (`model-fallback.ts:741`). So by the
        time the error reaches us the money reason is gone, and the only honest
        way to recover it is to re-read the budget.

        PROVIDER-AWARE BY CONSTRUCTION. Only owners whose inference Isol8 funds
        (`trial_local`, or paid `bedrock_claude`) can be reclassified. A
        ChatGPT-OAuth or BYO-key owner's 429 comes from THEIR provider on THEIR
        account; relabelling it would route them to an Isol8 billing page to fix
        someone else's quota. Same reasoning that made the wake-admission gate
        reuse gate_chat rather than a trial-only predicate.

        Returns `(code, message)` to substitute, or None to leave the upstream
        error exactly as it arrived — which is also the answer for a genuine
        Bedrock throttle against a funded owner.

        NOT "one DDB read". On the paid `bedrock_claude` branch this calls
        `metronome_gate.is_owner_blocked`, which is only a cheap fast-gate row
        read while that row is fresh — but the row's staleness window
        (`METRONOME_GATE_STALENESS_S`, default 60s) means any run longer than
        a minute (the common case) hits the stale path instead: a live
        Metronome `getNetBalance` over httpx (5s timeout), a `set_balance` DDB
        write, and then either `_maybe_restore_bedrock` (container read + a
        SECOND 5s `getNetBalance`) or `enforce_on_low_balance` (billing read +
        IAM policy delete) — up to ~10-15s of synchronous work. `_handle_message`
        awaits this before `_forward_to_frontends(error)` and the synthesized
        `run_ended` frame, and `_reader_loop` awaits `_handle_message`
        serially, so an unbounded call here stalls delivery of every
        subsequent event on this owner's gateway connection (other agents'
        streams included) behind it. Bounded below with `asyncio.wait_for` so
        the worst case is the timeout, not the full Metronome round trip(s).

        Never raises: this runs on the terminal error frame, and an exception
        here would drop the frame and hang the run in the UI. The reader loop's
        `except json.JSONDecodeError` around `_handle_message` catches nothing
        broader — an import failure here would propagate to
        `_on_reader_loop_exit` and tear down the owner's whole gateway
        connection, not just this one frame. So the imports are INSIDE the
        try too, not just the reads that follow them. The `asyncio.wait_for`
        timeout raises `TimeoutError` (a plain `Exception` subclass), which
        this same blanket `except Exception` below already swallows —
        degrading to "forward the upstream error unchanged", the documented
        fallback for any read failure on this path.
        """
        try:
            from core.repositories import billing_repo
            from core.services import trial_cap
            from core.services.metronome_gate import is_owner_blocked

            account = await billing_repo.get_by_owner_id(self.owner_id)
            if not account:
                return None

            if account.get("subscription_status") == "trial_local":
                reason = await trial_cap.reason_for_account(self.owner_id, account)
                if reason in BUDGET_BLOCK_MESSAGES:
                    return reason, BUDGET_BLOCK_MESSAGES[reason]
                return None

            if account.get("provider_choice") != "bedrock_claude":
                return None

            blocked = await asyncio.wait_for(is_owner_blocked(self.owner_id), timeout=_BUDGET_RECLASSIFY_TIMEOUT_S)
            if blocked:
                return "out_of_credits", BUDGET_BLOCK_MESSAGES["out_of_credits"]
            return None
        except Exception:
            logger.warning(
                "Budget reclassification read failed for owner %s — forwarding the upstream error unchanged",
                self.owner_id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _transform_agent_event(payload: dict) -> dict | None:
        """Extract streaming text, thinking, tool, and activity events from an OpenClaw agent event.

        OpenClaw's gateway coalesces the assistant/thinking streams at a
        hardcoded 150ms per run (fork src/gateway/server-chat.ts:1147,
        ``sendOrBufferAgentTextEvent``), so events arrive as ~150ms lumps
        carrying BOTH the accumulated ``text`` and the incremental ``delta``
        (coalesced events concatenate their deltas — see
        ``mergeBufferedAgentPayload``). We forward the delta plus the full
        text: ``content`` holds the delta with ``append: True``, and
        ``_full_content`` (internal, stripped by ``FrameSender``) holds the
        accumulated text so the sender can ship a full-text frame to a
        connection that has not yet seen this run (mid-run reconnect) or
        that dropped a frame. When the event carries no usable delta — or
        sets ``replace: true`` (the new text is not a prefix-extension of
        what was streamed) — we fall back to the legacy full-text
        cumulative-replacement frame.

        - ``stream: "assistant"`` events with text are forwarded as chunks.
        - ``stream: "reasoning"`` / ``"thinking"`` events are forwarded for real-time thinking.
        - ``stream: "tool"`` events are forwarded as tool_start/tool_end/tool_error,
          plus tool_progress for the mid-execution ``phase: "update"``.
        - ``stream: "command_output"`` events are forwarded as command_output
          (live stdout/stderr addressed to the matching tool pill).
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
            transformed = GatewayConnection._build_text_frame("chunk", data)

        elif stream in ("reasoning", "thinking"):
            transformed = GatewayConnection._build_text_frame("thinking", data)

        elif stream == "tool":
            phase = data.get("phase", "")
            name = data.get("name", "")
            tool_call_id = data.get("toolCallId", "")
            if phase == "update":
                # Live tool progress: incremental output emitted mid-execution
                # before the result phase. Upstream renders this via
                # formatToolOutput(data.partialResult); we forward it so the
                # running tool pill can show streaming output. An update attaches
                # to an EXISTING pill keyed by toolCallId, so a toolCallId is the
                # only thing it needs — name is optional here (the upstream
                # tool-event contract declares name as optional, and updates
                # address the pill by id, not name). Drop updates without a
                # toolCallId (nothing for the frontend to attach them to), but
                # never drop one merely for lacking a name.
                if tool_call_id:
                    transformed = {"type": "tool_progress", "toolCallId": tool_call_id}
                    if name:
                        transformed["tool"] = name
                    if "partialResult" in data:
                        transformed["partialResult"] = data["partialResult"]
            elif name:
                # start/result build or resolve a pill that needs the tool name,
                # so these phases still require it.
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

        elif stream == "command_output":
            # Streaming command stdout/stderr emitted mid-exec. Carries a
            # toolCallId so the frontend appends it to the matching live tool
            # pill. Without one there is nothing to address, so drop it.
            tool_call_id = data.get("toolCallId", "")
            if tool_call_id:
                transformed = {"type": "command_output", "toolCallId": tool_call_id}
                if "output" in data:
                    transformed["output"] = data["output"]

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
    def _build_text_frame(frame_type: str, data: dict) -> dict | None:
        """Build a ``chunk``/``thinking`` frame from an assistant/thinking event.

        Append-delta form when the event carries a usable delta (the common
        case): ``content`` is the delta, ``append: True``, and
        ``_full_content`` carries the accumulated text for the sender's
        per-connection full-text resync (see ``FrameSender``). Legacy
        cumulative full-text form when the delta is absent/empty or the event
        sets ``replace: true`` — the client replaces its buffer with
        ``content`` exactly as it always has.

        Returns ``None`` for events with no text at all (e.g. media-only).
        """
        text = data.get("text", "")
        delta = data.get("delta")
        if data.get("replace") is not True and isinstance(delta, str) and delta:
            return {
                "type": frame_type,
                "content": delta,
                "append": True,
                # `text` should always superset `delta`; the `or delta` guard
                # covers a hypothetical delta-only event so the resync form
                # can never ship an empty message.
                "_full_content": text or delta,
            }
        if text:
            return {"type": frame_type, "content": text}
        return None

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
        in-process fallback (DynamoDB is the single source of truth). A
        recipient that turns out to be Gone at send time is pruned by the
        sender via ``_prune_dead_conn`` so the GSI stops returning it.
        """
        recipients = await self._cached_owner_recipients()
        if recipients is None:
            # query failed; logged in the helper. No in-process fallback.
            return

        enqueued, skipped = _deliver_to_recipients(
            recipients, message, target_member_id, self._sender, on_gone=self._prune_dead_conn
        )
        # Log delivery summary for non-chunk messages (chunks are too frequent)
        msg_type = message.get("type", "?")
        if msg_type not in ("chunk",):
            logger.info(
                "[%s] forward type=%s target=%s enqueued=%d skipped=%d total=%d",
                self.user_id,
                msg_type,
                target_member_id or "broadcast",
                enqueued,
                skipped,
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
        # Drop any frames still queued for the dead connection (and its
        # append-continuity state) so the sender doesn't keep POSTing at a
        # Gone endpoint.
        self._sender.forget_connection(conn_id)
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

        Analytics only — the free-tier daily-spend counter is written by the
        Bedrock meter Lambda, which does its own per-``requestId`` dedup at
        the AWS layer. No runId dedup here.
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

    async def _resolve_agent_name(self, agent_id: str | None) -> str:
        """Best-effort display name for *agent_id* via a TTL-cached agents.list.

        Returns "" on any failure — the push falls back to a generic title.
        Uses the same request/response RPC idiom as ``_fetch_and_record_usage``
        (send + wait_for_response), since ``send_rpc`` only sends.
        """
        if not agent_id:
            return ""
        now = time.monotonic()
        if self._agent_names is not None and (now - self._agent_names_at) < _AGENT_NAME_TTL_S:
            return self._agent_names.get(agent_id, "")
        try:
            req_id = str(uuid.uuid4())
            await self.send_rpc(req_id, "agents.list", {})
            result = await self.wait_for_response(req_id, timeout=10)
            agents = result.get("agents", []) if isinstance(result, dict) else (result or [])
            names: Dict[str, str] = {}
            for a in agents:
                if not isinstance(a, dict):
                    continue
                aid = a.get("id") or a.get("agent_id")
                identity = a.get("identity") if isinstance(a.get("identity"), dict) else {}
                nm = a.get("name") or identity.get("name")
                if aid and nm:
                    names[aid] = nm
            self._agent_names = names
            self._agent_names_at = now
            return names.get(agent_id, "")
        except Exception:
            logger.warning("agents.list for push-name resolution failed", exc_info=True)
            return ""

    def _dispatch_push_notification(self, payload: dict, carried_member: str | None, agent_id: str | None) -> None:
        """Fire-and-forget push on a completed run (mirrors _record_usage_from_session).

        Audience = *carried_member* (the CANONICAL Clerk id of the dispatching
        member, carried on the active-run entry by ``mark_run_active``), or the
        owner (personal, whose key is agent:<id>:main with no member_id). It is
        deliberately NOT the ``target_member`` parsed from the echoed session key
        — OpenClaw lowercases that, and the users-table PK lookup in
        ``send_push_to_user`` is case-sensitive, so a lowercased id would miss.
        Only pushes to a Clerk user id; an unattributed org session (org_ owner,
        no carried member) silently no-ops.
        """
        push_user_id = carried_member or self.owner_id
        if not push_user_id or not push_user_id.startswith("user_"):
            return
        preview = self._extract_assistant_text(payload)
        if not preview:
            return
        session_key = payload.get("sessionKey")

        async def _send():
            try:
                agent_name = await self._resolve_agent_name(agent_id)
                body = preview if len(preview) <= 120 else preview[:119] + "…"
                data: Dict[str, str] = {}
                if agent_id:
                    data["agentId"] = agent_id
                if session_key:
                    data["sessionKey"] = session_key
                from core.services.push_service import send_push_to_user

                await send_push_to_user(push_user_id, agent_name or "New reply", body, data)
            except Exception:
                logger.exception("Push dispatch failed for user %s", push_user_id)

        task = asyncio.create_task(_send())
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

    def register_run_completion_callback(self, run_id: str, callback: Callable[[str, dict], Awaitable[None]]) -> None:
        """One-shot observer for *run_id*'s TRUE terminal chat event.

        *callback* is invoked ``await callback(state, payload)`` where
        ``state`` is ``"final" | "error" | "aborted"`` and ``payload`` is the
        raw terminal event body. Fired from ``_handle_message``'s TRUE-terminal
        branch — the same gate ``_dispatch_push_notification`` uses, so an
        interim `final` that only closes a tool-approval bracket (the run
        continues as a separate follow-up) does NOT resolve this callback.
        Overwrites any prior registration for the same *run_id* (callers mint
        a fresh run_id per dispatch, so a collision would only happen on a
        genuine retry, where "latest wins" is correct).
        """
        self._run_completion_callbacks[run_id] = callback

    def unregister_run_completion_callback(self, run_id: str) -> None:
        """Withdraw a registration made by ``register_run_completion_callback``
        without waiting for a terminal event to arrive and consume it.

        For a caller whose dispatch failed AFTER registering (e.g. chat.send
        itself raised, so the run never reached OpenClaw and no terminal
        event will EVER arrive for this *run_id*) — without this the entry
        (and its closure, which can hold references back to a repo/pool)
        leaks in ``_run_completion_callbacks`` until this ``GatewayConnection``
        is torn down. Idempotent — popping a *run_id* with nothing
        registered (already consumed, or never registered) is a silent
        no-op.
        """
        self._run_completion_callbacks.pop(run_id, None)

    def _dispatch_run_completion(self, run_id: str, state: str, payload: dict) -> None:
        """Pop and fire *run_id*'s registered completion callback, if any.

        Fire-and-forget (mirrors ``_dispatch_push_notification``): the
        callback runs as a tracked background task so a slow/failing
        observer (a DDB write) can never delay or break frame delivery to
        the browser. No-op when nothing is registered for *run_id* (the
        overwhelming common case — most runs have no observer).
        """
        callback = self._run_completion_callbacks.pop(run_id, None)
        if callback is None:
            return

        async def _run() -> None:
            try:
                await callback(state, payload)
            except Exception:
                logger.exception(
                    "Run-completion callback failed for user %s run_id=%s",
                    self.user_id,
                    run_id,
                )

        task = asyncio.create_task(_run())
        self._run_completion_tasks.add(task)
        task.add_done_callback(self._run_completion_tasks.discard)

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
        """Fetch session token counts via RPC and record usage.

        Analytics only. The free-tier daily-spend counter is NOT written
        here — the Bedrock invocation-log meter Lambda is its sole writer
        (it sees every invocation at the AWS layer, cache-inclusive and
        deduped on Bedrock's ``requestId``). A second in-band writer would
        double-count.
        """
        raw_cost_microcents: int | None = None
        model: str = "unknown"
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

            # OpenClaw computes estimatedCostUsd INCLUDING cache tokens, from
            # the same per-run snapshot as inputTokens (assigned, not
            # accumulated — see upstream session-usage.ts). sessions.list does
            # NOT serialize the cache token counts, so the token-based pricing
            # path sees cacheRead/cacheWrite=0 and captures only ~3% of real
            # cost (cache tokens are ~99.99% of volume). When present, bill on
            # estimatedCostUsd as the authoritative raw cost; otherwise fall
            # back to token-based pricing. 1 USD = 1_000_000 microcents.
            estimated_cost_usd = session.get("estimatedCostUsd")
            try:
                if estimated_cost_usd is not None and float(estimated_cost_usd) > 0:
                    raw_cost_microcents = round(float(estimated_cost_usd) * 1_000_000)
            except (TypeError, ValueError):
                raw_cost_microcents = None

            logger.info(
                "Session %s tokens: in=%d out=%d cache_r=%d cache_w=%d model=%s est_cost_usd=%s raw_microcents=%s",
                session_key,
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                model,
                estimated_cost_usd,
                raw_cost_microcents,
            )

            # Drop only a run with no token volume AND no billable cost. A
            # cache-heavy turn can report ~0 input/output tokens (cache counts
            # are omitted by sessions.list) but a real positive cost — keep it.
            if input_tokens <= 0 and output_tokens <= 0 and not raw_cost_microcents:
                logger.warning(
                    "Zero tokens + no cost in session %s for user %s — session keys: %s",
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
                    raw_cost_microcents=raw_cost_microcents,
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

            # Billing never happens here: the meter Lambda ingests every
            # Bedrock invocation straight to Metronome (rated, balanced,
            # enforced there). The record_usage write above is analytics
            # only — counters for the usage panel + the CE reconciliation.

        except Exception:
            put_metric("chat.session_usage.fetch.error")
            logger.exception("Failed to fetch session usage for user %s", self.user_id)

    async def _handle_message(self, data: dict) -> None:
        """Route an incoming gateway message.

        Event routing:
        - ``agent`` events: streaming text via ``_transform_agent_event``.
          OpenClaw coalesces these at a hardcoded 150ms per stream per run
          (fork server-chat.ts:1147) — they are NOT per-token.
        - ``chat`` events: only terminal states (final/error/aborted). Delta
          states are ignored because agent events already carry the same
          text (the ``chat`` stream has its own, separate 150ms throttle).
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
            # Paperclip issue/run sessions are NOT web chat: Paperclip activity
            # reaches Teams via its own event stream, so forwarding it here
            # leaks another surface's transcript into /chat — and because these
            # keys carry no member_id, the owner-keyed fan-out would BROADCAST
            # it to every member of the org. "unknown" covers any future
            # structurally-recognised-but-unclassified key the same way (fail
            # closed). Keyless control frames (sessions.updated, slash-command
            # finals) parse to an empty source and still forward.
            is_isolated_session = session_source in ("paperclip", "unknown")

            # Trailing opaque session id for the 4-part webchat key
            # (build_chat_session_key). None for the legacy 3-part "General"
            # key and for any non-webchat key. The client demuxes streamed
            # frames per session by matching this to its active session.
            session_parts = session_key.split(":") if session_key else []
            session_id = session_parts[3] if len(session_parts) == 4 and session_source == "webchat" else None

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
                stream = payload.get("stream", "")
                # Exclude text streams (assistant, reasoning, thinking) from
                # INFO logs — they fire every ~150ms for the whole run, so
                # logging would blow up CloudWatch volume on long responses.
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
                # Skip forwarding channel, cron, and Paperclip (issue/run)
                # events to the web UI. Each runs in its own isolated session
                # inside OpenClaw with its own delivery path — their streaming
                # events must not leak into a user's active web chat (a routine's
                # "done"/error would terminate or corrupt the user's streaming
                # session, and a member-less key would broadcast to the whole org).
                if is_channel_session or is_cron_session or is_isolated_session:
                    return
                transformed = self._transform_agent_event(payload)
                if transformed:
                    # Tag with agent_id so the frontend can route messages
                    # to the correct agent conversation and prevent cross-
                    # agent leakage when switching agents mid-stream.
                    event_agent_id = parsed_key.get("agent_id")
                    if event_agent_id:
                        transformed["agent_id"] = event_agent_id
                    if session_id is not None:
                        transformed["session_id"] = session_id
                    await self._forward_to_frontends(transformed, target_member)

            elif event_name == "chat":
                # Skip forwarding channel, cron, and Paperclip chat events to
                # the web UI (see the agent-event gate above).
                if is_channel_session or is_cron_session or is_isolated_session:
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
                # Captured before _forget_side_run drops the id, so the
                # chat.e2e.latency END guard at the bottom of this block can
                # tell a side (`/btw`) run from a primary run reliably.
                run_is_side = self._is_side_run(run_id)
                # START monotonic of the chat.e2e.latency SLO span (alarm W9),
                # reused from the active-run window the dispatcher opened via
                # mark_run_active (which stamps started_at BEFORE the chat.send
                # RPC is forwarded). Captured here, at the clear site, BEFORE
                # _clear_active_run pops the entry — the END emit at the bottom
                # of this block computes elapsed from it. None when this session
                # had no active-run window (e.g. a side run, an interim approval
                # bracket that stays active, or a run we never marked), in which
                # case no metric is emitted.
                primary_started_at: float | None = None
                # Canonical dispatch member id carried on the active-run entry
                # (mark_run_active), read back here for the run-completion push
                # audience. NOT the echoed/lowercased target_member — see
                # _dispatch_push_notification.
                carried_member: str | None = None
                if run_is_side:
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
                        active = self._active_runs.get(session_key.lower())
                        if active is not None:
                            primary_started_at = active.get("started_at")
                            carried_member = active.get("member_id")
                        # NOTE: _clear_active_run is DEFERRED to after the push
                        # dispatch in the is_true_terminal block below. Clearing
                        # here flips has_active_runs to False before the push task
                        # is spawned, so a concurrent _grace_close could tear the
                        # connection down mid-forward and cancel the reader before
                        # the push is ever created — losing the closed-app push.
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
                        if session_id is not None:
                            fwd["session_id"] = session_id
                        await self._forward_to_frontends(fwd, target_member)
                    # Forward the assistant text from chat.final as a NON-append
                    # full-text chunk. For agent runs the streamed chunks already
                    # painted the same text, so this is a visual no-op — and it
                    # doubles as the authoritative replace that corrects any
                    # delta a client may have missed mid-run (dropped frame,
                    # transient send failure). For slash commands (/help,
                    # /approve, etc.) there is no agent stream — this is the
                    # only path that delivers the response text at all.
                    text_content = self._extract_assistant_text(payload)
                    if text_content:
                        fwd = {"type": "chunk", "content": text_content}
                        if event_agent_id:
                            fwd["agent_id"] = event_agent_id
                        fwd["runId"] = run_id
                        if session_id is not None:
                            fwd["session_id"] = session_id
                        await self._forward_to_frontends(fwd, target_member)
                    fwd = {"type": "done"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    if session_id is not None:
                        fwd["session_id"] = session_id
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

                    # A budget refusal reaches us disguised as a provider
                    # throttle (see _budget_error_code). Recover the real reason
                    # so the user gets a limit card with an upgrade path instead
                    # of "wait for your quota to reset", which for a capped
                    # free-tier owner is advice they cannot act on.
                    upstream_msg: Optional[str] = None
                    if err_kind == "rate_limit":
                        money = await self._budget_error_code()
                        if money is not None:
                            upstream_msg = err_msg
                            err_kind, err_msg = money

                    put_metric(
                        "chat.error",
                        dimensions={
                            "reason": err_kind or "agent_error",
                            # Makes mislabelling VISIBLE. If we ever start
                            # reclassifying real AWS throttles, this spikes
                            # instead of failing silently.
                            "reclassified": "true" if upstream_msg else "false",
                        },
                    )
                    fwd = {"type": "error", "message": err_msg}
                    if err_kind:
                        fwd["code"] = err_kind
                    if upstream_msg:
                        # Keep the true upstream cause on the wire for logs and
                        # support. The card renders from `code`, so this never
                        # reaches the user as copy.
                        fwd["upstream_message"] = upstream_msg
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    if session_id is not None:
                        fwd["session_id"] = session_id
                    await self._forward_to_frontends(fwd, target_member)
                elif state == "aborted":
                    put_metric("chat.error", dimensions={"reason": "aborted"})
                    fwd = {"type": "error", "message": "Agent run was cancelled"}
                    if event_agent_id:
                        fwd["agent_id"] = event_agent_id
                    fwd["runId"] = run_id
                    if session_id is not None:
                        fwd["session_id"] = session_id
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
                    # Server-side completion observers (INV-5) — independent of
                    # frame forwarding below and of whether any browser tab is
                    # even watching. Fires for every TRUE terminal state
                    # (final/error/aborted), not just "final" (unlike the push
                    # dispatch, which is success-only).
                    self._dispatch_run_completion(run_id, state, payload)

                    run_ended: dict = {
                        "type": "run_ended",
                        "runId": run_id,
                        "outcome": "done" if state == "final" else state,
                    }
                    if event_agent_id:
                        run_ended["agent_id"] = event_agent_id
                    if session_id is not None:
                        run_ended["session_id"] = session_id
                    await self._forward_to_frontends(run_ended, target_member)

                    # Push a notification for a completed reply (fire-and-forget,
                    # off the reader-loop hot path — mirrors billing at the
                    # agent-event lifecycle site). Only on a successful final;
                    # error/aborted never push. Audience is the CANONICAL member
                    # carried on the active-run entry, not the echoed/lowercased
                    # target_member (which would PK-miss the users table).
                    # Skip side (`/btw`) runs: they carry no member (carried_member
                    # is None → would fall back to the owner) and are not the
                    # user-initiated primary reply v1 push is for.
                    if state == "final" and not run_is_side:
                        self._dispatch_push_notification(payload, carried_member, event_agent_id)

                    # Clear the non-side active-run window now — AFTER the push
                    # task has been created — so the teardown guard
                    # (has_active_runs) keeps the container connection alive
                    # through terminal processing and the push spawn. This closes
                    # the race where a concurrent _grace_close, seeing an empty
                    # _active_runs and no frontends, tore the connection down
                    # during the run_ended/done forwards and cancelled the reader
                    # before _dispatch_push_notification ran. Side runs never
                    # owned the entry, so they must not clear it.
                    if not run_is_side:
                        self._clear_active_run(session_key)

                # END of the chat.e2e.latency SLO span (alarm W9). Elapsed is
                # computed from the active-run window's started_at — stamped by
                # mark_run_active BEFORE the chat.send RPC was forwarded and
                # captured above at the clear site — so no parallel correlation
                # map is needed: the pool already records the run start. Emits on
                # EVERY primary-run TRUE terminal — final (success), error,
                # aborted — so the metric reflects real completions of all kinds.
                # Side (`/btw`) runs and interim approval brackets leave
                # primary_started_at None and emit nothing, so the p99 reflects
                # genuine user-perceived round-trips only. No dimensions: alarm
                # W9 queries chat.e2e.latency on {env,service} only (auto-injected
                # by put_metric); a custom dim would create a separate,
                # un-alarmed metric stream.
                # PASSIVE measurement: this runs AFTER all frame forwarding above
                # and is wrapped best-effort (never raises), so it cannot
                # alter/drop/delay/reorder any frame the frontend receives.
                if primary_started_at is not None:
                    try:
                        elapsed_ms = max(0.0, (time.monotonic() - primary_started_at) * 1000.0)
                        if elapsed_ms <= _E2E_LATENCY_CEILING_MS:
                            put_metric("chat.e2e.latency", value=elapsed_ms, unit="Milliseconds")
                        else:
                            # Late/stale terminal (e.g. re-delivered after a
                            # reconnect, or a hung run). Exclude it from the SLO
                            # p99 — a multi-minute outlier would false-fire W9 —
                            # but count the abandonment so it stays visible.
                            put_metric("chat.run.abandoned")
                    except Exception:
                        logger.warning(
                            "chat.e2e.latency emit failed for run_id=%s",
                            run_id,
                            exc_info=True,
                        )

            elif event_name == "chat.side_result":
                # `/btw` answers ride a distinct channel OpenClaw's web UI
                # renders as a side card; map it to a frontend chat-union frame
                # so it surfaces instead of being silently dropped by the
                # raw-forward path below. Side-channel only — it never touches
                # `_active_runs`/`_side_run_ids` or the primary run's window.
                if is_channel_session or is_cron_session or is_isolated_session:
                    return
                frame: dict = {
                    "type": "btw_result",
                    "question": payload.get("question", ""),
                    "text": payload.get("text", ""),
                    "runId": payload.get("runId"),
                }
                if payload.get("isError"):
                    frame["isError"] = True
                side_agent_id = parsed_key.get("agent_id")
                if side_agent_id:
                    frame["agent_id"] = side_agent_id
                await self._forward_to_frontends(frame, target_member)

            elif event_name == "sessions.changed":
                # OpenClaw's authoritative per-session run-state (from its live
                # abort-controller registry), broadcast on every run
                # start/end/transition. The payload embeds `hasActiveRun`, so we
                # read it directly — NO follow-up RPC (that would deadlock inside
                # the reader loop). Webchat-only + member-demuxed: a non-webchat
                # or member-less key must never reach the web UI (would leak
                # another surface or broadcast to the whole org). Deduped against
                # the last-forwarded value so message/transcript-driven
                # `sessions.changed` bursts don't spam identical frames.
                # Allow-list on webchat (not a deny-list of known-bad sources):
                # a malformed/unclassified key parses to source "" and must NOT
                # fall through — that would forward with target_member=None and
                # broadcast the frame to every one of the owner's chat
                # connections. Webchat keys always carry a member_id, so gating
                # here also guarantees per-member demux.
                if session_source != "webchat":
                    return
                active = bool(payload.get("hasActiveRun"))
                # Normalized to lowercase — see `mark_run_active` for why
                # every ``_active_runs``/``_run_state`` touch must agree on
                # casing (this dict is written only from event-side keys, but
                # normalize anyway rather than trust upstream's casing).
                run_state_key = session_key.lower()
                if self._run_state.get(run_state_key) == active:
                    return
                self._run_state[run_state_key] = active
                fwd_ra: dict = {"type": "run_active", "active": active}
                event_agent_id = parsed_key.get("agent_id")
                if event_agent_id:
                    fwd_ra["agent_id"] = event_agent_id
                if session_id is not None:
                    fwd_ra["session_id"] = session_id
                await self._forward_to_frontends(fwd_ra, target_member)

            else:
                # Forward other events as-is for SWR revalidation (broadcast to all)
                await self._forward_to_frontends(data)
            return

    async def _reader_loop(self) -> None:
        """Background task: read all messages from gateway WebSocket."""
        # Re-bind correlation ids onto the log contextvars: this loop runs in a
        # detached asyncio task spawned at connect, so the HTTP-middleware binds
        # never reached here. Bind the connection's owner_id as user_id so every
        # event-forwarding log line is queryable. Passive logging context only —
        # this touches no frame/event delivery. (See bind_log_context docstring.)
        bind_log_context(user_id=self.owner_id)
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
        # Durable liveness signal (R4 kill): the reader loop died with no
        # active runs, so is_alive is now False.
        self._note_gateway_liveness(connected=False)
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
                # Durable liveness signal (R4 kill): escalating past parked
                # to dead is the other path is_alive can flip False.
                self._note_gateway_liveness(connected=False)
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

    async def close(self) -> None:
        """Shut down: drain in-flight RPCs, cancel reader, close WebSocket."""
        self._emit_status_change("GATEWAY_DOWN_DEAD", "Gateway connection closed")
        # Durable liveness signal (R4 kill): an explicit close always ends
        # in is_alive == False, whether or not runs were in flight.
        self._note_gateway_liveness(connected=False)
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
        # Drain in-flight push-dispatch tasks too, so teardown doesn't cancel a
        # notification mid-send.
        if self._push_tasks:
            try:
                await asyncio.wait(self._push_tasks, timeout=5.0)
            except Exception:
                logger.exception(
                    "Error waiting for push tasks during close for user %s",
                    self.user_id,
                )
        # Drain in-flight run-completion observer tasks (INV-5 bootstrap
        # dispatcher) too, so teardown doesn't cancel a DDB completion write
        # mid-flight -- mirrors the push-dispatch drain above.
        if self._run_completion_tasks:
            try:
                await asyncio.wait(self._run_completion_tasks, timeout=5.0)
            except Exception:
                logger.exception(
                    "Error waiting for run-completion tasks during close for user %s",
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
        # One FrameSender for the whole pool: per-connectionId queues +
        # append-delta continuity must be global (a browser connection can
        # receive frames from a GatewayConnection AND the pool-level fan-out).
        self._sender = FrameSender(management_api)
        # Shared ConnectionService used by every GatewayConnection's owner-keyed
        # fan-out (query_by_owner on the by-owner-id GSI). Defaults to a fresh
        # instance — the same wiring as the module singleton elsewhere.
        self._conn_svc = conn_svc or ConnectionService()
        self._connections: Dict[str, GatewayConnection] = {}
        self._frontend_connections: Dict[str, Set[str]] = {}  # owner_id -> set of conn_ids
        self._lock = asyncio.Lock()
        self._grace_tasks: Dict[str, asyncio.Task] = {}
        # Per-owner client-liveness deadline (Layer 3 of scale-to-zero — see
        # `_CLIENT_DEADLINE_S`). One `asyncio.TimerHandle` per owner, armed by
        # `note_client_activity`. In-process, single-backend-instance scope —
        # same posture as `_grace_tasks` (desiredCount=1 for the backend
        # service), so a timer armed on this task is the only one that can
        # exist for a given owner.
        self._client_deadline_handles: Dict[str, asyncio.TimerHandle] = {}
        # Per-user reconnect attempt timestamps for the `_RECONNECT_FLOOR_S`
        # storm guard. Tracks the start of the *most recent* reconnect (not
        # success / failure) so a single bad container can't burn the
        # backend's open-handshake budget at 1s cadence.
        self._last_reconnect_at: Dict[str, float] = {}
        # Per-user auth-failure backoff (see `_AUTH_BACKOFF_BASE_S`).
        # `_auth_backoff_until` is a monotonic deadline; `_auth_failures`
        # counts consecutive auth-shaped connect failures driving the
        # exponential hold. A successful connect clears both.
        self._auth_backoff_until: Dict[str, float] = {}
        self._auth_failures: Dict[str, int] = {}

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

    def has_active_runs(self, owner_id: str) -> bool:
        """Whether the owner's gateway connection has an in-flight webchat run.

        Thin pool-level delegate over the connection's ``has_active_runs``
        property. No connection at all → False (nothing running here). Used as
        a scale-to-zero veto: never stop a container mid-turn. Webchat-only —
        a Paperclip/Teams run is instead caught by the live-chat-connection
        count (the Teams UI holds a chat-typed WS).
        """
        conn = self._connections.get(owner_id)
        return conn is not None and conn.has_active_runs

    def is_parked(self, owner_id: str) -> bool:
        """Whether the owner's connection is parked (WS dropped mid-run,
        presumed alive). No connection → False. Scale-to-zero veto: a parked
        connection means OpenClaw was streaming recently and will reconnect
        lazily — killing it would abort the run it is presumed to still hold.
        """
        conn = self._connections.get(owner_id)
        return conn is not None and conn._parked

    def mark_run_active(
        self,
        user_id: str,
        session_key: str,
        agent_id: str | None = None,
        member_id: str | None = None,
    ) -> None:
        """Public entry point for the chat dispatcher.

        Records that a chat.send is being issued for *session_key* on the
        per-user gateway connection. Silently no-ops when no connection exists
        for *user_id* yet — and NOTHING else populates ``_active_runs`` (inbound
        events only touch/clear an existing entry), so a mark issued before the
        connection exists is simply lost. The chat dispatcher therefore calls
        ``ensure_connection`` before marking on every non-side send, which
        guarantees a connection here. Idempotent.

        *member_id* is the CANONICAL Clerk id of the dispatching member (carried
        onto the active-run entry for the run-completion push audience).
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.mark_run_active(session_key, agent_id, member_id=member_id)

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

    def register_run_completion_callback(
        self, user_id: str, run_id: str, callback: Callable[[str, dict], Awaitable[None]]
    ) -> bool:
        """Public entry point for a caller that needs to observe a specific
        dispatched run's TRUE terminal (final/error/aborted) without being
        the browser tab that sent it — e.g. the server-owned bootstrap
        dispatcher (INV-5, spec PR-6).

        Returns False (registers nothing) when no connection exists for
        *user_id*. Callers should call ``ensure_connection`` first (mirrors
        ``mark_run_active``'s own contract) so this only returns False if the
        connection vanished between the two calls.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return False
        conn.register_run_completion_callback(run_id, callback)
        return True

    def unregister_run_completion_callback(self, user_id: str, run_id: str) -> None:
        """Public entry point mirroring ``register_run_completion_callback`` —
        see ``GatewayConnection.unregister_run_completion_callback``. Silently
        no-ops when no connection exists for *user_id* (nothing to withdraw).
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.unregister_run_completion_callback(run_id)

    async def gate_chat(self, *, user_id: str, owner_id: str, session_key: str) -> dict:
        """Pre-chat hard-stop for card-3 (bedrock_claude) users and the
        free-tier ``trial_local`` daily cost cap.

        Layers:

        1. **Subscription status.** The billing row's ``subscription_status``
           must be ``active``, ``trialing`` or ``trial_local``. Cards 1 + 2
           used to bypass the gate entirely because their LLM cost lives on
           the user's own provider account — but the *container* itself is on
           us, so a canceled subscription should still block chat. Without
           this, an org/personal user whose ``subscription.deleted`` webhook
           landed could keep talking through the gateway indefinitely on
           cards 1 + 2.
        2. **Free-tier $3/day cost cap.** A ``trial_local`` owner's model
           spend is pure Isol8 cost (no BYOK/OAuth/credit backstop). This is
           a **read-only** check of the daily-spend counter the Bedrock
           invocation-log meter Lambda maintains — the backend never writes
           to it and places no reserve, because the meter counts every
           invocation at the AWS layer and enforces the real stop (revoke the
           owner's inference profile + ``desiredCount=0``). Blocking here is
           purely UX: a clean 402 card instead of a burnt turn. A sub-check
           first blocks with ``code="trial_expired"`` when ``trial_end`` has
           passed. Both are gated on ``subscription_status == "trial_local"``
           — the single trial predicate — so a paid owner mid-upgrade-window
           with a stale ``trial_end`` is never told their trial expired, and
           never capped.
        3. **Card-3 balance.** When ``provider_choice`` is
           ``bedrock_claude``, the owner must not be blocked by the
           Metronome fast-gate read (webhook-fed flag + lazy getNetBalance
           refresh — see ``core/services/metronome_gate.py``). Cards 1
           + 2 don't draw from prepaid credits, so they're not gated
           on balance.

        All layers key on ``owner_id`` (org_id for org context, user_id
        for personal). ``user_id`` is the chatting member, retained on
        the signature for logging / future per-user telemetry.
        ``session_key`` is retained for logging / future per-turn telemetry.

        Returns:
            ``{"blocked": False}`` when chat may proceed;
            ``{"blocked": True, "code": <code>, "message": ...}``
            when the chat must NOT be forwarded to OpenClaw.

        A raised exception (the cap read is a network call) is intentionally
        NOT caught here — it propagates to the caller, which treats a gate
        error the same as any other chat-send failure (notifies the client,
        never forwards to OpenClaw). Swallowing it into ``{"blocked": False}``
        would be the unsafe direction for a cost cap.
        """
        from core.observability.product_analytics import capture_once_per_owner_per_day
        from core.repositories import billing_repo
        from core.services import trial_cap
        from core.services.metronome_gate import is_owner_blocked
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
                "message": BUDGET_BLOCK_MESSAGES["subscription_inactive"],
            }

        # --- Layer 2: free-tier $3/day cost cap ----------------------
        # Keyed on subscription_status == "trial_local" — the single trial
        # predicate. The container row is NOT consulted: the counter is
        # written by the meter Lambda (which keys on the owner's inference
        # profile, not on any container-row stamp), and the moment billing
        # flips to paid the owner is off the cap by definition — their
        # spend lands on Metronome, not here.
        #
        # Guarded on the status we already have in hand from Layer 1 so a
        # paid owner's chat never pays for the extra billing read
        # ``reason_for_account`` does. That helper self-gates on the same
        # predicate, so this is belt-and-braces, not a second contract.
        if account is not None and account.get("subscription_status") == "trial_local":
            # ONE predicate, shared with the provision gate, the container-start
            # chokepoint, /container/recover and the Teams run guards, so no door
            # can disagree about whether a trial owner has runway. This branch
            # used to inline its own trial_end comparison — a third copy of that
            # arithmetic, and exactly the drift the shared helper exists to
            # prevent. ``account`` is already in hand from Layer 1, so
            # reason_for_account spends no extra DDB read.
            reason = await trial_cap.reason_for_account(owner_id, account)
            if reason == trial_cap.TRIAL_EXPIRED:
                return {
                    "blocked": True,
                    "code": "trial_expired",
                    "message": BUDGET_BLOCK_MESSAGES["trial_expired"],
                }
            if reason == trial_cap.DAILY_CAP:
                # Trial funnel telemetry: first block of the UTC day per
                # owner (dedup below) — every later turn re-hits this same
                # branch until midnight or upgrade, so without the dedup a
                # single owner would spam one event per chat send.
                capture_once_per_owner_per_day(owner_id=owner_id, event="trial_cap_reached")
                return {
                    "blocked": True,
                    "code": "trial_daily_cap",
                    "message": BUDGET_BLOCK_MESSAGES["trial_daily_cap"],
                }

        # --- Layer 3: card-3 credit balance --------------------------
        # provider_choice lives on the billing_accounts row (per owner);
        # credits also pool at the owner level so org members all draw
        # from one balance funded by the
        # admin. Keying on user_id meant org members saw balance=0 and
        # got blocked at chat with code=out_of_credits.
        if account is None or account.get("provider_choice") != "bedrock_claude":
            return {"blocked": False}

        if await is_owner_blocked(owner_id):
            return {
                "blocked": True,
                "code": "out_of_credits",
                "message": BUDGET_BLOCK_MESSAGES["out_of_credits"],
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
            sender=self._sender,
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
                self._respect_auth_backoff(user_id)
                self._respect_reconnect_floor(user_id)
                try:
                    await conn.reopen()
                except GatewayConnectAuthError as exc:
                    self._record_auth_failure(user_id, exc.retry_after_ms)
                    raise
                self._clear_auth_backoff(user_id)
            elif conn is not None and not conn.is_connected:
                self._respect_reconnect_floor(user_id)
                await conn.close()
                conn = None
            if conn is None:
                self._respect_auth_backoff(user_id)
                try:
                    conn = await self._create_connection(user_id, ip, token)
                except GatewayConnectAuthError as exc:
                    self._record_auth_failure(user_id, exc.retry_after_ms)
                    raise
                self._clear_auth_backoff(user_id)
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

    def _respect_auth_backoff(self, user_id: str) -> None:
        """Raise if *user_id* is inside an auth-failure hold.

        Mirrors `_respect_reconnect_floor`, but for auth-shaped connect
        rejections: retrying those with the same credentials cannot succeed
        and each attempt counts toward the gateway's 10-in-60s lockout, so
        the hold is minutes, not seconds.
        """
        until = self._auth_backoff_until.get(user_id, 0.0)
        if not until:
            return
        remaining = until - time.monotonic()
        if remaining > 0:
            put_metric("gateway.connection", dimensions={"event": "auth_backoff"})
            raise RuntimeError(
                f"Gateway reconnect suppressed for user {user_id} — auth backoff active for another {remaining:.1f}s"
            )

    def _record_auth_failure(self, user_id: str, retry_after_ms: int | None) -> None:
        """Arm/extend the auth hold after a ``GatewayConnectAuthError``.

        Exponential in consecutive failures (`_AUTH_BACKOFF_BASE_S` doubling,
        capped at `_AUTH_BACKOFF_CAP_S`); the gateway's own retryAfterMs (the
        remaining lockout window on AUTH_RATE_LIMITED) dominates when longer.
        """
        n = self._auth_failures.get(user_id, 0) + 1
        self._auth_failures[user_id] = n
        hold_s = float(min(_AUTH_BACKOFF_BASE_S * (2 ** (n - 1)), _AUTH_BACKOFF_CAP_S))
        if retry_after_ms:
            hold_s = max(retry_after_ms / 1000.0, hold_s)
        self._auth_backoff_until[user_id] = time.monotonic() + hold_s
        logger.warning(
            "Gateway auth failure for user %s (consecutive=%d) — holding reconnects %.0fs",
            user_id,
            n,
            hold_s,
        )

    def _clear_auth_backoff(self, user_id: str) -> None:
        self._auth_backoff_until.pop(user_id, None)
        self._auth_failures.pop(user_id, None)

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
            # Forget the run-active dedup so a tab that (re)connects or opens
            # mid-run gets a fresh `run_active` on the next `sessions.changed`
            # instead of having the still-true state suppressed as a duplicate —
            # otherwise its status bar sits dark through the active window. The
            # frontend clears its own `containerRunActive` on disconnect, so
            # without this the server would never re-light it. (OpenClaw's own
            # client re-reads sessions.list on reconnect for the same reason.)
            conn.reset_run_state_dedup()

        # Cancel grace period if one is running
        grace = self._grace_tasks.pop(user_id, None)
        if grace and not grace.done():
            grace.cancel()

        # Arm/reset the client-liveness deadline (Layer 3) — a fresh
        # registration is activity too, not just pings.
        self.note_client_activity(user_id)

    def note_client_activity(self, owner_id: str) -> None:
        """(Re)arm the per-owner client-liveness deadline.

        Called on every ping (`websocket_chat.ws_message`'s ping handler,
        alongside the existing `_touch_liveness` DDB stamp) and from
        `add_frontend_connection` — both are "the browser is still there"
        signals. Cancels any existing timer and reschedules a fresh one
        `_CLIENT_DEADLINE_S` out; on expiry `_on_client_deadline_expired`
        force-clears the owner's frontend set and flows into the same tail
        `remove_frontend_connection` takes on a normal disconnect (arms
        `_grace_close`, Task 12's veto ladder).

        No-ops when the owner has no registered frontend connection — the
        timer must exist only while there is a connection set for it to
        eventually clear (guards a stray/late ping racing a disconnect).
        Also no-ops (best-effort) when there is no running event loop, which
        never happens in production (both callers run inside a live FastAPI
        request coroutine) but keeps this safe to call from synchronous test
        contexts.
        """
        self._cancel_client_deadline(owner_id)
        if not self._frontend_connections.get(owner_id):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        handle = loop.call_later(_CLIENT_DEADLINE_S, self._fire_client_deadline, owner_id)
        self._client_deadline_handles[owner_id] = handle

    def _cancel_client_deadline(self, owner_id: str) -> None:
        """Cancel + drop the owner's client-liveness timer, if any.

        Idempotent — safe to call whether or not a timer is currently armed.
        Called on every path that empties (or might empty)
        `_frontend_connections[owner_id]`: `remove_frontend_connection` (when
        the set drains) and `close_user` (defense in depth — always cancels,
        even off a path that didn't go through `remove_frontend_connection`).
        """
        handle = self._client_deadline_handles.pop(owner_id, None)
        if handle is not None:
            handle.cancel()

    def _fire_client_deadline(self, owner_id: str) -> None:
        """`TimerHandle` callback (sync, runs on the event loop thread).

        Just spawns the async expiry check — `_on_client_deadline_expired`
        needs to await `remove_frontend_connection`'s downstream
        `_grace_close` task creation and touches `_frontend_connections`,
        neither of which a bare callback can do directly.
        """
        self._client_deadline_handles.pop(owner_id, None)
        asyncio.create_task(self._on_client_deadline_expired(owner_id))

    async def _on_client_deadline_expired(self, owner_id: str) -> None:
        """No ping (or reconnect) arrived for `_CLIENT_DEADLINE_S` — presume
        a lost `$disconnect` and recover it.

        Re-reads `_frontend_connections` at fire time rather than trusting
        any captured state, so a set that already emptied via a different
        path (e.g. a dead-connection prune inside `GatewayConnection`) is a
        harmless no-op here — nothing to force-clear, nothing to arm.

        When the set is non-empty, force-clears it by routing every
        registered connection id through `remove_frontend_connection` — the
        SAME function a normal `$disconnect` calls — so the last removal
        arms `_grace_close` exactly the way Task 12 already verified. This
        deliberately does not duplicate any of that veto logic; it only
        recovers the missing disconnect signal.
        """
        fcs = self._frontend_connections.get(owner_id)
        if not fcs:
            return
        logger.warning(
            "Client liveness deadline expired for owner=%s (no ping in %ss) — "
            "force-clearing %d stale frontend connection(s)",
            owner_id,
            _CLIENT_DEADLINE_S,
            len(fcs),
        )
        put_metric("gateway.client_deadline.expired")
        for conn_id in list(fcs):
            self.remove_frontend_connection(owner_id, conn_id)

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

        if not fcs:
            # No frontends left. Normal disconnects supersede the client-
            # liveness deadline entirely — it must never outlive an empty
            # connection set (it would otherwise fire later and no-op, but
            # cancelling promptly keeps `_client_deadline_handles` honest).
            self._cancel_client_deadline(user_id)
            # Start grace period if no frontend connections remain AND a
            # gateway connection actually exists to tear down.
            if user_id in self._connections:
                self._grace_tasks[user_id] = asyncio.create_task(self._grace_close(user_id))

    def broadcast_to_owner(self, owner_id: str, event: str, payload: dict) -> None:
        """Push a generic ``{type: "event"}`` frame to every frontend WS
        registered for *owner_id*.

        Pool-level (keyed off ``_frontend_connections`` only) rather than
        routed through a ``GatewayConnection`` — deliberately independent of
        whether an OpenClaw gateway connection exists yet, since a billing or
        Paperclip write (e.g. ``needs_signup`` -> ``provisioning``) can land
        before the container itself is up. Mirrors
        ``GatewayConnection._emit_status_change``'s enqueue shape but fans out
        to the whole owner rather than one connection's own frontend set.

        Never raises: ``FrameSender.enqueue`` never raises, and a Gone
        recipient is pruned via ``on_gone`` the same way every other
        pool-level push handles it. See ``core.services.status_events.
        publish_status_changed`` for the primary caller.
        """
        message = {"type": "event", "event": event, "payload": payload}

        async def _on_gone(conn_id: str) -> None:
            fcs = self._frontend_connections.get(owner_id)
            if fcs:
                fcs.discard(conn_id)
            put_metric("gateway.frontend.prune")

        for conn_id in list(self._frontend_connections.get(owner_id, ())):
            self._sender.enqueue(conn_id, message, on_gone=_on_gone)

    async def _grace_close(self, user_id: str) -> None:
        """Wait the grace period, then close the gateway connection if still idle.

        Guard: while a run is still in flight for this owner, HOLD the connection
        open (bounded by _PUSH_GRACE_CEILING_S) so the reader loop still observes
        run_ended and can fire the completion push — even with no client attached
        (e.g. a phone that sent then closed). Clients re-appearing cancels this
        task via remove/add_frontend_connection, so the loop also exits when a
        client returns.

        Then, for free-tier (``trial_local``) owners, a second and longer grace
        follows the WS close: if the owner still has no live chat clients, no
        in-flight webchat run and no parked connection, the container itself is
        stopped (desiredCount=0) via ``_maybe_stop_trial_container``. Event-driven
        only — this task exists solely because a last-disconnect event armed it;
        the wake path is the existing GET /container/status auto-retry (cap-gated).

        ``conn.has_active_runs`` is a ``@property``; the pool-level
        ``self.has_active_runs(owner)`` is a method. Both are used below.
        """
        try:
            await asyncio.sleep(_GRACE_PERIOD)
            waited = 0.0
            while not self._frontend_connections.get(user_id) and waited < _PUSH_GRACE_CEILING_S:
                conn = self._connections.get(user_id)
                if conn is None or not conn.has_active_runs:
                    break
                await asyncio.sleep(_ACTIVE_RUN_POLL_S)
                waited += _ACTIVE_RUN_POLL_S
            if self._frontend_connections.get(user_id):
                return  # a client came back while we held the connection open

            # Decide trial-ness AND run-state BEFORE close_user: afterwards the
            # GatewayConnection is popped, so has_active_runs/is_parked would
            # always read "no connection" and could never see a run that was live
            # at close time. A run still in flight here (the push-grace ceiling
            # lapsed with one running) means the container is actively working
            # even though the last tab closed — the trial-stop arm must NOT be
            # armed on top of it. (Lazy billing_repo import matches gate_chat:
            # core.containers imports this module, so a top-level repo import
            # would be circular.)
            from core.repositories import billing_repo

            # A failed billing read must not skip close_user (the behavior this
            # method has always guaranteed) — treat the owner as non-trial and
            # fall through to the plain WS close.
            try:
                account = await billing_repo.get_by_owner_id(user_id)
            except Exception:
                logger.exception("grace-close billing read failed user=%s", user_id)
                account = None
            is_trial = bool(account) and account.get("subscription_status") == "trial_local"
            run_in_flight = self.has_active_runs(user_id) or self.is_parked(user_id)
            await self.close_user(user_id)
            if not (is_trial and not run_in_flight):
                return
            # close_user popped this task out of _grace_tasks; re-register it so a
            # reconnect's add_frontend_connection cancels the extended sleep
            # cleanly (defense in depth with the fire-time frontend re-check in
            # _maybe_stop_trial_container). The grace task now outlives the WS
            # close — nothing else keys teardown off _grace_tasks membership.
            self._grace_tasks[user_id] = asyncio.current_task()
            try:
                await asyncio.sleep(_TRIAL_STOP_GRACE_S)
                await self._maybe_stop_trial_container(user_id)
            finally:
                if self._grace_tasks.get(user_id) is asyncio.current_task():
                    self._grace_tasks.pop(user_id, None)
        except asyncio.CancelledError:
            pass

    async def _maybe_stop_trial_container(self, owner_id: str, trigger: str = "grace") -> None:
        """Stop a trial container that provably has nobody using it.

        Every check must pass; any doubt = don't stop (over-preserve, never
        over-kill — the container costs ~$0.02/hr, a killed run costs trust).
        Re-checks ``trial_local`` at fire time so an owner who converted to a
        paid plan during the grace is never stopped out from under their first
        paid session (the stop deliberately keys on ``subscription_status``,
        not a container-row stamp that could lag the conversion).

        Also called directly by Tasks 13/14 with a different *trigger* metric
        dimension. Those callers may invoke while a GatewayConnection still
        exists, so the has_active_runs/is_parked vetoes below are load-bearing
        there. In the grace path they always read "no connection" (close_user
        already ran) — the meaningful vetoes there are the frontend re-check,
        the trial re-read, and the cross-task live-chat count.
        """
        try:
            if self._frontend_connections.get(owner_id):
                return  # a client reconnected on this task
            from core.repositories import billing_repo

            account = await billing_repo.get_by_owner_id(owner_id)
            if not account or account.get("subscription_status") != "trial_local":
                return
            if self.has_active_runs(owner_id) or self.is_parked(owner_id):
                return  # in-flight/parked webchat run — never kill mid-turn
            if await self._conn_svc.count_live_chat_connections(owner_id) > 0:
                return  # cross-task truth: another backend task holds a live tab
            if await self._conn_svc.has_live_operator_connection(owner_id):
                # A Paperclip run is executing RIGHT NOW. has_active_runs above
                # is webchat-only and is blind to it: a trial owner who posts a
                # Teams comment and closes their tab has a live Friday run the
                # pool never sees. Stopping the container under it kills a
                # legitimate turn — over-kill, which this ladder forbids. The
                # adapter opens its WS in execute() and closes it when the run
                # ends, so the connection's existence IS the run.
                return
            from core.containers import get_ecs_manager

            await get_ecs_manager().stop_user_service(owner_id, reason="trial_idle")
            put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": trigger})
        except Exception:
            logger.exception("trial scale-to-zero check failed owner=%s", owner_id)

    async def close_user(self, user_id: str) -> None:
        """Close gateway connection for a user."""
        conn = self._connections.pop(user_id, None)
        if conn:
            put_metric("gateway.connection", dimensions={"event": "disconnect"})
            await conn.close()
        self._frontend_connections.pop(user_id, None)
        self._grace_tasks.pop(user_id, None)
        # This pops the whole frontend-connection entry regardless of
        # whether it was already emptied via `remove_frontend_connection` —
        # defense in depth so the client-liveness deadline can never outlive
        # the connection set it protects (see `_cancel_client_deadline`).
        self._cancel_client_deadline(user_id)
        # Drop the reconnect-floor entry so a fresh user (e.g. someone who
        # signed out and back in on the same backend task) doesn't inherit
        # a stale throttle from the previous session.
        self._last_reconnect_at.pop(user_id, None)
        # Same rationale as the reconnect-floor pop above: a torn-down user
        # must not leave auth-backoff state behind for a future incarnation
        # (a reprovision mints a fresh token, so a stale hold would suppress
        # its first legitimate connect).
        self._auth_backoff_until.pop(user_id, None)
        self._auth_failures.pop(user_id, None)

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

        async def _on_gone(conn_id: str) -> None:
            await self._prune_owner_connection(owner_id, conn_id)

        _deliver_to_recipients(recipients, message, target_member_id, self._sender, on_gone=_on_gone)

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
        """Shutdown: close all connections concurrently, then drain delivery."""
        await asyncio.gather(
            *(self.close_user(uid) for uid in list(self._connections.keys())),
            return_exceptions=True,
        )
        # Flush queued frontend frames (close() above enqueues final
        # status_change frames) so shutdown doesn't cancel deliveries mid-send.
        try:
            await asyncio.wait_for(self._sender.flush(), timeout=5.0)
        except Exception:
            logger.warning("Frame sender flush timed out during shutdown", exc_info=True)

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

    # Paperclip adapter sessions: agent:<agentId>:paperclip(:issue:<id>|:run:<id>)?
    # (paperclip/packages/adapters/openclaw-gateway/src/server/execute.ts
    # resolveSessionKey). A real member id is always a Clerk "user_..." id,
    # never the literal "paperclip", so this marker is unambiguous. Must check
    # BEFORE webchat so the 3-part bare "agent:<id>:paperclip" fallback key
    # isn't misread as an org member named "paperclip". These sessions are
    # delivered to Teams via Paperclip's own event stream, NOT the web /chat
    # fan-out — see the source gate in the agent/chat event handlers.
    if parts[2] == "paperclip":
        result = {"agent_id": agent_id, "source": "paperclip"}
        if len(parts) >= 5 and parts[3] == "issue":
            result["issue_id"] = parts[4]
        elif len(parts) >= 5 and parts[3] == "run":
            result["run_id"] = parts[4]
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

    # Webchat named session: agent:<agentId>:<userId>:<opaqueSessionId>
    # (4-part, server-built by build_chat_session_key). parts[2] is the Clerk
    # user_id (member routing target); parts[3] is the client-minted UUID.
    # Cron/paperclip 4-part keys are already handled above, and channel keys are
    # >=5 parts (group/room) or 6 (DM), so an exactly-4-part key here is
    # unambiguously a webchat session. Must classify as webchat so it is NOT
    # treated as isolated in _handle_message (which would drop streamed frames).
    if len(parts) == 4:
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
