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
from typing import Any, Dict, Optional, Set

from websockets import connect as ws_connect

from core.gateway.operator_rpc import GATEWAY_PORT, GatewayConnectAuthError, perform_signed_connect
from core.observability.logging import bind_log_context
from core.observability.product_analytics import schedule_trial_first_agent_response
from core.observability.metrics import put_metric, gauge
from core.repositories import channel_link_repo
from core.services.connection_service import ConnectionService
from core.dynamodb import utc_now_iso

logger = logging.getLogger(__name__)

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

# How long an endpoint we recreated a connection AWAY from stays "superseded"
# — a resolution still pointing at it is treated as stale and cannot roll the
# newer connection back onto the dead task. This is NOT the race timer: the race
# it guards (concurrent callers straddling task replacements, all serialized on
# the pool lock) resolves in seconds. Records are stamped at DETACH and kept as a
# bounded HISTORY (not one slot), so a resolution straddling several fast
# replacements stays fenced. The clock exists ONLY as the IP-recycling backstop:
# if the VPC hands this owner's *next* task the exact same /32 (rare; task
# lifetimes are minutes-to-hours), the stale record must lapse so the recycled IP
# is trusted again. 5 minutes sits an order of magnitude above worst-case
# connect()+lock-queue yet far below any recycling timescale.
#
# Residual ceiling: a stale resolution queued behind MANY serial cold connects on
# the pool-wide lock (each ~20-40s) could in principle outlive even this window.
# That queue only reaches minutes when a single backend task cold-dials a large
# fleet after a deploy — the global-lock-across-handshake serialization is a
# separate, tracked scalability limit (the in-process WS fanout note), not this
# fence's to solve. Fully decoupling the fence from wall clock needs a
# caller-carried resolve timestamp threaded through ~15 mostly-single-shot call
# sites — disproportionate to a deploy-only deep tail whose worst case is a ≤300s
# self-healing reconnect, versus the indefinite-timeout outage on main this whole
# path fixes.
_SUPERSEDED_IP_TTL_S = 300.0

# Cap on the per-owner superseded-endpoint history. 8 covers far more consecutive
# task replacements than any real churn strands in-flight resolutions for; the
# oldest entry is evicted past this.
_SUPERSEDED_HISTORY_MAX = 8

# Auth-failure backoff, per user, alongside the reconnect floor. The gateway
# locks a client IP+credential scope out for 5 minutes after 10 failed auth
# attempts in 60s (AUTH_RATE_LIMITED); the 5s floor alone allows 12
# attempts/min, so retrying a stale token at floor cadence trips the lockout
# and bricks the owner's connectivity. Exponential per-user hold
# (base × 2^(n-1), capped), with the gateway's own retryAfterMs dominant
# when longer. Cleared by the next successful connect.
_AUTH_BACKOFF_BASE_S = 60
_AUTH_BACKOFF_CAP_S = 300


class GatewayReconnectThrottled(RuntimeError):
    """Refused by the reconnect floor.

    TRANSIENT by construction: the floor is ``_RECONNECT_FLOOR_S`` seconds and
    nothing else has to change for the next attempt to succeed. Callers that
    retry on a ladder should treat this as retryable.
    """


class GatewayAuthBackoff(RuntimeError):
    """Suppressed by the auth-failure hold.

    The semantic OPPOSITE of ``GatewayReconnectThrottled``, despite both
    guarding ``ensure_connection``: retrying with the same credentials cannot
    succeed, and each attempt counts toward the gateway's 10-in-60s lockout.
    Callers must NOT treat this as retryable.

    These are distinct types precisely so ``except RuntimeError`` can never
    catch both — that conflation latched bootstrap dispatch at ``failed`` on a
    5-second cooldown.
    """


class _GatewayReaderClosed(Exception):
    """Sentinel handed to ``_on_reader_loop_exit`` when the reader loop ends on
    a CLEAN peer close (``ConnectionClosedOK`` ends the ``async for`` WITHOUT
    raising), so the teardown log distinguishes a graceful close from a genuine
    transport error while still routing through the same teardown path."""


# How stale `last_event_at` on an active run must be before the parked
# connection counts as "agent appears stuck" for observability. Set by spec
# §6 — we don't ESCALATE on this (deferred follow-up); we only
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

# Frame types whose full-text resync payload is accumulated per run.
_RUN_TEXT_TYPES = frozenset({"chunk", "thinking"})
_ROOM_SAFE_FRAME_TYPES = frozenset(
    {
        "chunk",
        "thinking",
        "done",
        "error",
        "run_ended",
        "run_active",
        "tool_start",
        "tool_end",
        "tool_error",
        "tool_progress",
        "command_output",
        "agent_activity",
        "btw_result",
    }
)

# Bound on the per-connection run-text accumulator. Entries self-clean on the
# run's chat terminal; this cap is only a safety net for a run whose terminal
# never arrives (container killed mid-stream, reader loop torn down). Well
# above any realistic count of concurrent runs on one owner's container.
_RUN_TEXT_CAP = 64


class _RunText:
    """Per-run accumulation behind the ``_full_content`` resync payload.

    OpenClaw resets its assistant text buffer at every ``message_start``
    (``resetAssistantMessageState`` → ``state.deltaBuffer = ""``), and every
    tool call ends one assistant message and starts another. So the ``text``
    on a ``stream: "assistant"`` agent event is cumulative within the CURRENT
    assistant message only, while our chunk stream — and the frontend bubble
    it paints — is keyed by ``runId`` and spans the whole run.

    ``blocks`` maps frame type → ``(prefix, last_snapshot)``: the text of the
    run's earlier assistant messages, plus the last per-message snapshot seen.
    A snapshot that no longer extends the previous one is a message boundary,
    so the previous snapshot retires into the prefix.

    Deliberately NOT delta accumulation: keeping upstream's own per-message
    snapshot as the tail means a delta we never received (a gateway seq gap)
    still heals on the next event, because ``text`` carries the whole current
    message rather than only what changed.
    """

    __slots__ = ("blocks", "resync")

    def __init__(self) -> None:
        self.blocks: dict[str, tuple[str, str]] = {}
        # Set when the gateway reports a seq gap: frames were dropped between
        # the container and us, so the next text frame must ship as a full
        # replace rather than an append.
        self.resync = False


# A chat run whose terminal arrives later than this is a hung/stale outlier
# (e.g. a terminal re-delivered after a reconnect), NOT a real latency
# datapoint — emitting its elapsed would inflate the chat.e2e.latency p99 and
# can false-fire W9 (p99 > 20s SLO). 10 min is far past any legitimate chat
# round-trip; over-ceiling terminals bump chat.run.abandoned instead.
_E2E_LATENCY_CEILING_MS = 10 * 60 * 1000  # 10 minutes


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
        self._conn_svc = conn_svc or ConnectionService()
        self._on_activity = on_activity
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_rpcs: Dict[str, asyncio.Future] = {}
        self._closed = False
        self._billing_tasks: Set[asyncio.Task] = set()
        # Fire-and-forget run-completion push tasks (mirrors _billing_tasks):
        # tracked so close() can drain them instead of cancelling a send.
        self._push_tasks: Set[asyncio.Task] = set()
        # Fire-and-forget product-analytics emit tasks. Tracked SEPARATELY from
        # _billing_tasks and CANCELLED (not drained) on close: an emit's
        # Paperclip enrichment can run to its full 5s budget, and sharing the
        # billing drain window would let best-effort analytics starve usage
        # writes at shutdown.
        self._analytics_tasks: Set[asyncio.Task] = set()
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
        # Room-routing metadata may outlive `_active_runs` briefly when an
        # ingress POST times out after OpenClaw accepted it. It is deliberately
        # separate so uncertain delivery never keeps a connection "active".
        self._room_run_context: Dict[str, Dict[str, Any]] = {}
        # Recently terminal run IDs retain their session key so idempotent abort
        # still passes current room authorization after the active entry is
        # cleared. Bounded insertion-order map; runtime state remains
        # process-local like the gateway connection itself.
        self._terminal_run_ids: Dict[str, str] = {}
        self._live_publish_lock = asyncio.Lock()
        # Lightbulb room session keys this connection has asked the gateway to
        # stream (`sessions.messages.subscribe`). See
        # `subscribe_room_session` for why a room run is invisible without it.
        # Re-sent by `connect()`, so a reconnect keeps streaming.
        self._room_session_subs: set[str] = set()
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
        # runId -> per-run text accumulation for the full-text resync payload
        # (see `_RunText` and `_rewrite_run_full_text`). Keyed by runId, so two
        # concurrent runs on a shared org container — or a `/btw` side run
        # alongside its primary — never read each other's text. Insertion-
        # ordered, used as a bounded FIFO; entries self-clean on the run's
        # chat terminal.
        self._run_text: Dict[str, _RunText] = {}
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
        # Latest scheduled _write_liveness task. Retained for two reasons:
        # a strong reference (the loop keeps only weak task refs), and so
        # shutdown can DRAIN the terminal delete — close() only schedules
        # it, and an undrained delete cancelled by lifespan teardown leaves
        # a dead process's row falsely "ready" for the freshness window.
        self._pending_liveness_write: Optional[asyncio.Task] = None

    async def drain_liveness_write(self, timeout_s: float = 5.0) -> None:
        """Wait (bounded, non-cancelling) for the most recently scheduled
        liveness write, if any.

        The terminal disconnect delete serializes behind any in-flight put
        via ``_liveness_lock``, so awaiting the LAST scheduled task is
        sufficient to know the row reflects this connection's final state.
        Bounded so a wedged DynamoDB call can never hold a close path past
        the lifespan's shutdown deadline, and SHIELDED so the timeout only
        stops the waiting — the write task itself keeps running rather
        than being cancelled mid-put/delete. Never raises: write failures
        are already swallowed inside the service methods."""
        task = self._pending_liveness_write
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout_s)
            except Exception:
                pass

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
        self._pending_liveness_write = asyncio.create_task(self._write_liveness(connected=connected))

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
        # AppSync carries a reconcile hint, not a second status authority.
        from core.services.status_events import publish_status_changed

        publish_status_changed(self.owner_id)

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

    def mark_run_active(
        self,
        session_key: str,
        agent_id: str | None = None,
        member_id: str | None = None,
        event_kind: str | None = None,
        thread_root_message_id: str | None = None,
    ) -> None:
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
        if not hasattr(self, "_room_run_context"):
            self._room_run_context = {}
        existing = self._active_runs.get(key)
        if existing is not None:
            existing["started_at"] = now
            existing["last_event_at"] = now
            if agent_id:
                existing["agent_id"] = agent_id
            if member_id:
                existing["member_id"] = member_id
            if event_kind and existing.get("event_kind") is None:
                # First kind wins for the life of the run — an ambient
                # room_event re-mark must not flip an in-flight addressed
                # run's suppression state (see `_publish_room_frame`).
                existing["event_kind"] = event_kind
            if thread_root_message_id and existing.get("thread_root_message_id") is None:
                existing["thread_root_message_id"] = thread_root_message_id
                self._room_run_context[key] = {"thread_root_message_id": thread_root_message_id}
            return
        self._active_runs[key] = {
            "started_at": now,
            "last_event_at": now,
            "agent_id": agent_id or "",
            "run_id": None,
            "event_kind": event_kind,
            "thread_root_message_id": thread_root_message_id,
            # Canonical dispatch member id for the run-completion push audience.
            "member_id": member_id,
            # Set when OpenClaw emits an approval-requested agent event for this
            # session; consumed by `_is_true_terminal` so the interim chat
            # `final` that closes the approval bracket does NOT end the run.
            "awaiting_approval": False,
        }
        self._room_run_context[key] = {"thread_root_message_id": thread_root_message_id}
        while len(self._room_run_context) > 256:
            self._room_run_context.pop(next(iter(self._room_run_context)), None)

    def _touch_run(self, session_key: str, run_id: str | None = None) -> None:
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
            if run_id:
                run["run_id"] = run_id

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

    def _clear_active_run(
        self,
        session_key: str,
        run_id: str | None = None,
        *,
        preserve_room_context: bool = False,
    ) -> None:
        """Pop an active run and retain its terminal ID for idempotent abort."""
        key = session_key.lower()
        active = self._active_runs.pop(key, None)
        if not preserve_room_context:
            getattr(self, "_room_run_context", {}).pop(key, None)
        terminal_id = run_id or (active or {}).get("run_id")
        if terminal_id:
            self._terminal_run_ids[terminal_id] = session_key.lower()
            while len(self._terminal_run_ids) > 256:
                self._terminal_run_ids.pop(next(iter(self._terminal_run_ids)), None)
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
        src/agents/embedded-agent-subscribe.handlers.tools.ts, on an
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
        room_context = getattr(self, "_room_run_context", {}).get(session_key.lower())
        if room_context is not None:
            room_context["awaiting_approval"] = True

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
        key = session_key.lower()
        run = self._active_runs.get(key) or getattr(self, "_room_run_context", {}).get(key)
        if run is not None and run.get("awaiting_approval"):
            # Suppress Run A's genuine terminal final so the per-session window
            # survives the approval; the separate follow-up run's final clears it.
            run["awaiting_approval"] = False
            active = self._active_runs.get(key)
            if active is not None:
                active["awaiting_approval"] = False
            room_context = getattr(self, "_room_run_context", {}).get(key)
            if room_context is not None:
                room_context["awaiting_approval"] = False
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
        for room_session_key in list(self._room_session_subs):
            await self._send_room_session_subscribe(room_session_key)
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
        cleared by the server (`message-handler.ts`), leaving the
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
        (shared with the one-shot operator calls).

        Capabilities we opt into, both load-bearing:

        - ``tool-events`` — OpenClaw streams ``stream:"tool"`` events only to
          clients declaring it; without it the gateway silently drops them.
        - ``approvals`` — ``canDeliverApprovals`` needs an approval scope AND
          proof the client renders that approval kind: an internal approval
          runtime, a client id in one of its hardcoded sets (control-ui, the
          native apps, tui), or this capability. We connect as ``gateway-client``
          and match none of those sets, so without it we are not an approval
          route at all. That is worse than a dropped event: with no route the
          gateway calls ``manager.expire(id, "no-approval-route")`` and hands the
          agent ``decision: null`` immediately. It covers plugin and system-agent
          approvals — which the frontend already subscribes to — while exec
          approvals stay moot because ``tools.exec.mode`` is ``"full"`` and none
          is ever created.
        """
        await perform_signed_connect(
            self._ws,
            owner_id=self.user_id,
            token=self.token,
            caps=["tool-events", "approvals"],
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
                    {"last_gateway_handshake_at": utc_now_iso()},
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
        `sessions.subscribe` (fork src/gateway/server-methods/sessions.ts),
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

    async def subscribe_room_session(self, session_key: str) -> None:
        """Ask the gateway to stream a Lightbulb room session to this connection.

        Channel-initiated runs are registered with ``isControlUiVisible: false``
        — the fork derives it from the message provider, and only the internal
        ``webchat`` channel counts as visible (agent-9.3
        src/auto-reply/reply/agent-runner-execution.ts:172-187,
        src/utils/message-channel-constants.ts). Every broadcast that gate
        protects is then skipped for our room runs: tool events
        (src/gateway/server-chat.ts:1619), assistant/item text (:1699) and the
        whole chat terminal (:746). So the backend saw NOTHING for a room run —
        no ``agent`` events, no ``chat`` final — and ``_publish_room_frame``
        never fired even though ``mark_run_active`` had opened the window.

        The one door a hidden run leaves open is ``sessionMessageSubscribers``:
        a client that called ``sessions.messages.subscribe`` for the session key
        gets the tool branch (:1641), the hidden text mirror (:1698) and the
        chat terminal (:742) delivered to it by connection id. One RPC per room
        session, `operator.read`-scoped, which we already hold.

        Idempotent per connection and fire-and-forget for the same reason
        ``_subscribe_session_events`` is: the ack carries nothing we need.
        """
        if not session_key or session_key in self._room_session_subs:
            return
        self._room_session_subs.add(session_key)
        await self._send_room_session_subscribe(session_key)

    async def _send_room_session_subscribe(self, session_key: str) -> None:
        if self._ws is None:
            return
        frame = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "sessions.messages.subscribe",
            "params": {"key": session_key},
        }
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:
            logger.warning(
                "sessions.messages.subscribe send failed user=%s",
                self.user_id,
                exc_info=True,
            )

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
        discards the gate's own message (`model-fallback.ts`). So by the
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
        hardcoded 150ms per run (fork src/gateway/server-chat.ts,
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
        - ``stream: "run_status" | "compaction"`` ride the same envelope but
          describe the run's CURRENT STATE (startup phase, history compaction)
          rather than an event; the frontend routes them to the status strip.

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

        elif stream in ("run_status", "compaction"):
            # Two "what is it doing right now" streams that used to fall out of
            # this transform as None.
            #
            # run_status: the typed startup phases OpenClaw emits between the
            # send and the first token (agent-run-status-events.ts). A closed
            # enum — preparing_workspace | naming_worktree | creating_worktree |
            # running_setup | provisioning_environment | preparing_context |
            # starting_model — so the frontend maps it to copy rather than
            # printing a raw enum at someone who does not know what a worktree
            # is. Only the workspace/context/model three reach our fleet; the
            # worktree and environment phases come from project sessions and
            # worker environments, neither of which we run.
            #
            # compaction: history compaction start/end. A long session stalls
            # for tens of seconds here with no explanation, and a compaction
            # that FAILS (completed: false) otherwise reads as an unexplained
            # pause followed by a possibly context-lost reply.
            #
            # Both ride the existing agent_activity envelope — no new frame
            # type, no protocol change — and `detail` carries the whole payload
            # so the frontend can tell a completed compaction from a failed one.
            # The frontend routes these two activities to the run-status strip
            # instead of a transcript pill: a phase is the run's current state,
            # not an event worth logging.
            phase = data.get("phase", "")
            if phase:
                transformed = {
                    "type": "agent_activity",
                    "activity": stream,
                    "title": phase,
                    "detail": data,
                }

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

    def _run_text_state(self, run_id: str) -> "_RunText":
        """Get-or-create the run's text accumulator, evicting oldest past the cap."""
        state = self._run_text.get(run_id)
        if state is None:
            state = _RunText()
            self._run_text[run_id] = state
            while len(self._run_text) > _RUN_TEXT_CAP:
                self._run_text.pop(next(iter(self._run_text)), None)
        return state

    def _rewrite_run_full_text(self, frame: dict) -> None:
        """Widen a text frame's authoritative text from per-MESSAGE to per-RUN.

        ``_build_text_frame`` copies OpenClaw's ``text`` verbatim, but that
        field is cumulative only within the current assistant message —
        upstream clears its buffer at every ``message_start``, and every tool
        call starts a new assistant message. Our chunk stream is keyed by
        ``runId`` and the frontend paints one bubble per run, so on the normal
        agentic shape (text → tool_use → text) the authoritative text a
        resyncing client receives would be the LAST block only: everything the
        agent said before the last tool call disappears from the bubble.

        Two frame shapes carry authoritative text and both are widened here:

        - append form — ``content`` is the delta, ``_full_content`` is the
          snapshot ``FrameSender`` ships to a client with no continuity for
          this run (mid-run reconnect, a second tab, a dropped frame);
        - cumulative-replacement form — ``content`` IS the snapshot, and the
          client replaces its whole buffer with it.

        The delta-append path itself was never wrong, so this changes nothing
        for a client that saw every frame. It also does not touch the
        ``chat``-terminal replace, which already carries run-scoped text from
        the gateway's own accumulator and stays the end-of-run authority.
        """
        frame_type = frame.get("type")
        run_id = frame.get("runId")
        if frame_type not in _RUN_TEXT_TYPES or not run_id:
            return
        key = "_full_content" if "_full_content" in frame else "content"
        snapshot = frame.get(key)
        if not isinstance(snapshot, str):
            return

        state = self._run_text_state(run_id)
        prefix, last = state.blocks.get(frame_type, ("", ""))
        # A snapshot that no longer extends the previous one is a new assistant
        # message (or an upstream `replace`): retire the old text into the
        # prefix so the run's bubble keeps it.
        if last and not snapshot.startswith(last):
            prefix += last
        state.blocks[frame_type] = (prefix, snapshot)
        frame[key] = prefix + snapshot

        # Seq gap: the gateway told us it dropped frames, so this client's
        # append stream has a hole. Ship the widened text as a replace instead
        # of a delta — `prefix + snapshot` is whole-run authoritative, so the
        # hole closes on the first frame after the gap.
        if state.resync:
            state.resync = False
            if frame.pop("append", None):
                frame["content"] = frame.pop("_full_content")

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

    async def _publish_room_frame(self, session_key: str, frame: dict) -> None:
        parsed = parse_lightbulb_session_key(session_key)
        if parsed is None or frame.get("type") not in _ROOM_SAFE_FRAME_TYPES:
            return
        key = session_key.lower()
        active = self._active_runs.get(key)
        room_context = active or getattr(self, "_room_run_context", {}).get(key)
        if (active or {}).get("event_kind") == "room_event" and frame.get("type") in {"chunk", "thinking"}:
            return
        from core.repositories import messaging_repo
        from core.services.live_events import get_live_event_publisher
        from core.services.messaging import make_live_event

        meta = await messaging_repo.get_room(parsed["room_id"])
        if meta is None or meta.get("ownerId") != self.owner_id:
            return
        member = await messaging_repo.get_member(parsed["room_id"], "agent", parsed["agent_id"])
        if member is None:
            return
        normalized = {**frame}
        normalized.pop("_full_content", None)
        run_id = normalized.get("runId") or (active or {}).get("run_id")
        event = make_live_event(
            "room.run.frame",
            {
                "roomId": parsed["room_id"],
                "agentId": parsed["agent_id"],
                "runId": run_id,
                "threadRootMessageId": (room_context or {}).get("thread_root_message_id"),
                "frame": normalized,
            },
            room_id=parsed["room_id"],
        )
        try:
            async with self._live_publish_lock:
                await get_live_event_publisher().publish_room(parsed["room_id"], event)
        except Exception:
            logger.warning(
                "room run-frame publish failed owner=%s frame_type=%s",
                self.owner_id,
                normalized.get("type"),
                exc_info=True,
            )

    def _private_audience(self, session_key: str) -> str | None:
        run = self._active_runs.get(session_key.lower()) if session_key else None
        member = (run or {}).get("member_id")
        if isinstance(member, str) and member.startswith("user_"):
            return member
        return self.owner_id if self.owner_id.startswith("user_") else None

    async def _publish_user_event(self, event_type: str, payload: Any, session_key: str = "") -> None:
        user_id = self._private_audience(session_key)
        from core.services.live_events import get_live_event_publisher, publish_owner_users
        from core.services.messaging import make_live_event

        event = make_live_event(event_type, payload)
        try:
            if user_id is not None:
                await get_live_event_publisher().publish_user(self.owner_id, user_id, event)
            else:
                # No addressable member (org owner, container-originated event
                # with no dispatching human) — fan out to every active member
                # stream instead of silently dropping the event.
                await publish_owner_users(self.owner_id, event)
        except Exception:
            logger.warning(
                "private runtime event publish failed owner=%s event_type=%s",
                self.owner_id,
                event_type,
                exc_info=True,
            )

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

    def _emit_trial_first_response(self) -> None:
        """Fire-and-forget ``trial_first_agent_response`` (see
        ``schedule_trial_first_agent_response``). Same shape as
        ``_emit_run_completed``: gate synchronously, spawn, never raise."""
        try:
            task = schedule_trial_first_agent_response(self.owner_id)
        except Exception:
            logger.exception("trial_first_agent_response scheduling failed for user %s", self.user_id)
            return
        if task is not None:
            self._analytics_tasks.add(task)
            task.add_done_callback(self._analytics_tasks.discard)

    def _emit_run_completed(self, payload: dict, parsed_key: dict, phase: str) -> None:
        """Fire-and-forget ``agent_run_completed`` product event (mirrors
        ``_record_usage_from_session``: classify synchronously, spawn the
        enrichment+capture task; close() cancels rather than drains it).
        Must never raise into the reader loop.
        """
        try:
            from core.services import run_analytics

            task = run_analytics.schedule_run_completed(
                owner_id=self.owner_id,
                parsed=parsed_key,
                payload=payload,
                phase=phase,
            )
        except Exception:
            logger.exception("agent_run_completed scheduling failed for user %s", self.user_id)
            return
        if task is not None:
            self._analytics_tasks.add(task)
            task.add_done_callback(self._analytics_tasks.discard)

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

    def _dispatch_push_notification(
        self, payload: dict, carried_member: str | None, agent_id: str | None, state: str = "final"
    ) -> None:
        """Fire-and-forget push on a terminal run (mirrors _record_usage_from_session).

        *state* is ``"final"`` or ``"error"`` (aborted never pushes — the user
        cancelled the run themselves). A final with no assistant text (tool-only
        run) still pushes with a generic body: the run ending is the signal, not
        the prose.

        Audience = *carried_member* (the CANONICAL Clerk id of the dispatching
        member, carried on the active-run entry by ``mark_run_active``), or the
        owner (personal, whose key is agent:<id>:main with no member_id). It is
        deliberately NOT the ``target_member`` parsed from the echoed session key
        — OpenClaw lowercases that, and the users-table PK lookup in
        ``send_push_to_user`` is case-sensitive, so a lowercased id would miss.
        Only pushes to a Clerk user id; an unattributed org session (org_ owner,
        no carried member) drops with a metric so the gap is visible.
        """
        push_user_id = carried_member or self.owner_id
        if not push_user_id or not push_user_id.startswith("user_"):
            put_metric("push.unattributed_drop")
            return
        if state == "error":
            err = payload.get("errorMessage") or payload.get("error")
            if isinstance(err, dict):
                err = err.get("message")
            preview = str(err) if err else "Agent run failed"
        else:
            preview = self._extract_assistant_text(payload) or "Finished a run"
        session_key = payload.get("sessionKey")

        async def _send():
            try:
                agent_name = await self._resolve_agent_name(agent_id)
                if state == "error":
                    title = f"{agent_name} run failed" if agent_name else "Agent run failed"
                else:
                    title = agent_name or "New reply"
                body = preview if len(preview) <= 120 else preview[:119] + "…"
                data: Dict[str, str] = {}
                if agent_id:
                    data["agentId"] = agent_id
                if session_key:
                    data["sessionKey"] = session_key
                from core.services.push_service import send_push_to_user

                await send_push_to_user(push_user_id, title, body, data)
            except Exception:
                logger.exception("Push dispatch failed for user %s", push_user_id)

        task = asyncio.create_task(_send())
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

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

    async def _fetch_session_row(self, session_key: str) -> dict | None:
        """The one gateway session row for *session_key*, or ``None``.

        Fast path is ``sessions.describe {key}``: one keyed lookup for the
        row we actually want, instead of ``sessions.list {}``, whose cost
        grows with the owner's entire session store and which ran on the
        owner's own container after EVERY chat turn.

        Verified against the pinned fork tag ``v2026.8.1-isol8.3``:
        ``sessions.describe`` is scope ``operator.read`` and has existed
        since ``<=2026.7`` (``src/gateway/methods/core-descriptors.ts``), so
        it is safe on every image tag in the fleet; it responds
        ``{"session": null}`` on a miss; and it builds its row with the same
        ``buildGatewaySessionRow`` ``sessions.list`` uses, so every field
        read below is present. ``estimatedCostUsd`` is in fact better
        resolved there — list rows are built ``lightweightListRow: true``
        (``session-utils-list.ts``), which reads only the stored value,
        while describe also runs the transcript-usage fallback.

        The scan survives as the COLD path, for a describe MISS only.
        OpenClaw's session-key folding is one-directional — a mixed-case key
        also matches its lowercased legacy alias, never the reverse
        (``foldedSessionKeyAliasCandidates``) — so the case-insensitive
        compare the scan does is not reproducible with a keyed lookup, and
        dropping it would silently lose usage analytics for those rows. A
        describe that RAISES is deliberately NOT retried against the scan:
        it propagates to the caller's handler, exactly as a failing
        ``sessions.list`` did.
        """
        req_id = str(uuid.uuid4())
        await self.send_rpc(req_id, "sessions.describe", {"key": session_key})
        result = await self.wait_for_response(req_id, timeout=10)
        session = result.get("session") if isinstance(result, dict) else None
        if isinstance(session, dict):
            return session

        req_id = str(uuid.uuid4())
        await self.send_rpc(req_id, "sessions.list", {})
        result = await self.wait_for_response(req_id, timeout=10)
        if not isinstance(result, dict):
            logger.warning(
                "sessions.list returned non-dict for user %s: type=%s",
                self.user_id,
                type(result).__name__,
            )
            return None

        sessions = [s for s in result.get("sessions", []) if isinstance(s, dict)]
        session_key_lower = session_key.lower()
        for s in sessions:
            if str(s.get("key", "")).lower() == session_key_lower:
                put_metric("chat.session_usage.describe.miss")
                logger.warning(
                    "sessions.describe missed %s for user %s; recovered by scan",
                    session_key,
                    self.user_id,
                )
                return s

        logger.warning(
            "Session %s not found for user %s (describe + %d-row scan). Available: %s",
            session_key,
            self.user_id,
            len(sessions),
            [s.get("key", "?") for s in sessions[:5]],
        )
        return None

    async def _fetch_and_record_usage(self, session_key: str, member_user_id: str) -> None:
        """Fetch session token counts via RPC and record usage.

        Analytics only. The free-tier daily-spend counter is NOT written
        here. That counter has TWO writers, both outside this function: the
        bedrock-gate settles it first (in JavaScript, on the hot inference
        path), and the Bedrock invocation-log meter Lambda backstops by
        counting only the invocations the gate missed. Exactly-once is
        guaranteed by the shared ``requestId`` dedup table
        (``lightbulb-{env}-bedrock-invocation-dedup``), not by single-writer
        discipline — a third in-band writer here would still double-count.
        """
        raw_cost_microcents: int | None = None
        model: str = "unknown"
        try:
            session = await self._fetch_session_row(session_key)
            if not session:
                return

            input_tokens = int(session.get("inputTokens", 0) or 0)
            output_tokens = int(session.get("outputTokens", 0) or 0)
            cache_read = int(session.get("cacheRead", 0) or 0)
            cache_write = int(session.get("cacheWrite", 0) or 0)
            model = session.get("model") or "unknown"

            # OpenClaw computes estimatedCostUsd INCLUDING cache tokens, from
            # the same per-run snapshot as inputTokens (assigned, not
            # accumulated — see upstream session-usage.ts). The gateway session
            # row does NOT serialize the cache token counts, so token pricing
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
            # are omitted from the row) but a real positive cost — keep it.
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
        """Resolve RPCs and project only authorized native runtime events."""
        if data.get("type") == "res":
            req_id = data.get("id")
            future = self._pending_rpcs.get(req_id)
            if future and not future.done():
                if data.get("ok"):
                    future.set_result(data.get("payload", {}))
                else:
                    error = data.get("error", {})
                    future.set_exception(RuntimeError(json.dumps(error) if isinstance(error, dict) else str(error)))
            return
        if data.get("type") != "event":
            return

        event_name = data.get("event", "")
        payload = data.get("payload")
        if not isinstance(payload, dict) or event_name in {"health", "tick"}:
            return
        session_key = payload.get("sessionKey") if isinstance(payload.get("sessionKey"), str) else ""
        run_id = payload.get("runId") if isinstance(payload.get("runId"), str) else None
        if session_key:
            self._touch_run(session_key, run_id)
        parsed_key = _parse_session_key(session_key) if session_key else {}
        room = parse_lightbulb_session_key(session_key) if session_key else None

        if event_name == "agent":
            stream = payload.get("stream")
            stream_data = payload.get("data")
            if not isinstance(stream_data, dict):
                return
            if (
                stream == "approval"
                and stream_data.get("phase") == "requested"
                and stream_data.get("status") in {"pending", "unavailable"}
                and session_key
                and not self._is_side_run(run_id)
            ):
                self._mark_awaiting_approval(session_key)
            if stream == "lifecycle" and stream_data.get("phase") in {"end", "error"}:
                if stream_data["phase"] == "end":
                    self._record_usage_from_session(payload)
                self._emit_run_completed(payload, parsed_key, stream_data["phase"])
            if room is None:
                return
            transformed = self._transform_agent_event(payload)
            if transformed is not None:
                self._rewrite_run_full_text(transformed)
                transformed["agent_id"] = room["agent_id"]
                await self._publish_room_frame(session_key, transformed)
            return

        if event_name == "chat":
            # NOTE: no `if room is None: return` here. Push notifications,
            # active-run cleanup, and e2e-latency metrics must fire for every
            # terminal chat run, not just canonical-room ones.
            # `_publish_room_frame` already no-ops internally for a
            # non-room session key, so the room-channel publish stays
            # correctly gated without an explicit check here.
            agent_id = parsed_key.get("agent_id")
            state = payload.get("state")
            if state not in {"final", "error", "aborted"} or not run_id:
                return
            self._run_text.pop(run_id, None)
            run_is_side = self._is_side_run(run_id)
            primary_started_at: float | None = None
            carried_member: str | None = None
            if run_is_side:
                self._forget_side_run(run_id)
                is_true_terminal = True
            else:
                is_true_terminal = bool(session_key) and self._is_true_terminal(session_key, state)
                if is_true_terminal:
                    active = self._active_runs.get(session_key.lower())
                    if active is not None:
                        primary_started_at = active.get("started_at")
                        carried_member = active.get("member_id")

            if state == "final":
                put_metric("chat.message.count")
                if is_true_terminal and not run_is_side and (self._extract_assistant_text(payload) or "").strip():
                    self._emit_trial_first_response()
                thinking = self._extract_thinking_text(payload)
                if thinking:
                    await self._publish_room_frame(
                        session_key,
                        {"type": "thinking", "content": thinking, "agent_id": agent_id, "runId": run_id},
                    )
                text = self._extract_assistant_text(payload)
                if text:
                    await self._publish_room_frame(
                        session_key,
                        {"type": "chunk", "content": text, "agent_id": agent_id, "runId": run_id},
                    )
                await self._publish_room_frame(
                    session_key,
                    {"type": "done", "agent_id": agent_id, "runId": run_id},
                )
            elif state == "error":
                message = payload.get("errorMessage") or payload.get("error") or "Agent run failed"
                if isinstance(message, dict):
                    message = message.get("message") or "Agent run failed"
                error_kind = payload.get("errorKind")
                upstream_message = None
                if error_kind == "rate_limit":
                    money = await self._budget_error_code()
                    if money is not None:
                        upstream_message = str(message)
                        error_kind, message = money
                frame = {"type": "error", "message": str(message), "agent_id": agent_id, "runId": run_id}
                if error_kind:
                    frame["code"] = error_kind
                if upstream_message:
                    frame["upstream_message"] = upstream_message
                await self._publish_room_frame(session_key, frame)
            else:
                await self._publish_room_frame(
                    session_key,
                    {
                        "type": "error",
                        "message": "Agent run was cancelled",
                        "agent_id": agent_id,
                        "runId": run_id,
                    },
                )

            if is_true_terminal:
                await self._publish_room_frame(
                    session_key,
                    {
                        "type": "run_ended",
                        "runId": run_id,
                        "outcome": "done" if state == "final" else state,
                        "agent_id": agent_id,
                    },
                )
                if state in {"final", "error"} and not run_is_side:
                    self._dispatch_push_notification(payload, carried_member, agent_id, state)
                if not run_is_side:
                    self._clear_active_run(session_key, run_id)
            if primary_started_at is not None:
                elapsed_ms = max(0.0, (time.monotonic() - primary_started_at) * 1000.0)
                if elapsed_ms <= _E2E_LATENCY_CEILING_MS:
                    put_metric("chat.e2e.latency", value=elapsed_ms, unit="Milliseconds")
                else:
                    put_metric("chat.run.abandoned")
            return

        if event_name == "chat.side_result" and room is not None:
            await self._publish_room_frame(
                session_key,
                {
                    "type": "btw_result",
                    "question": payload.get("question", ""),
                    "text": payload.get("text", ""),
                    "isError": bool(payload.get("isError")),
                    "agent_id": room["agent_id"],
                    "runId": run_id,
                },
            )
            return

        if event_name == "sessions.changed" and room is not None:
            active_row = self._active_runs.get(session_key.lower())
            active_run_id = (active_row or {}).get("run_id")
            if not active_run_id:
                return
            active = _is_session_run_active(payload)
            key = session_key.lower()
            if self._run_state.get(key) == active:
                return
            self._run_state[key] = active
            await self._publish_room_frame(
                session_key,
                {"type": "run_active", "active": active, "agent_id": room["agent_id"], "runId": active_run_id},
            )
            return

        nested_request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        event_session = session_key or nested_request.get("sessionKey", "")
        if event_name in {"exec.approval.requested", "plugin.approval.requested"}:
            await self._publish_user_event("runtime.approval.requested", payload, event_session)
        elif event_name in {"exec.approval.resolved", "plugin.approval.resolved"}:
            await self._publish_user_event("runtime.approval.resolved", {"id": payload.get("id")}, event_session)
        elif event_name == "question.requested":
            await self._publish_user_event("runtime.question.requested", payload, event_session)
        elif event_name == "question.resolved":
            await self._publish_user_event("runtime.question.resolved", payload, event_session)
        elif event_name == "progressCard.changed":
            await self._publish_user_event(
                "runtime.progress.changed",
                {"sessionKey": payload.get("sessionKey"), "revision": payload.get("revision")},
                event_session,
            )
        else:
            resources = {
                "agents.changed": "agents",
                "config.changed": "config",
                "models.changed": "models",
                "skills.changed": "skills",
                "tools.changed": "tools",
            }
            resource = resources.get(event_name)
            if resource:
                await self._publish_user_event("runtime.resource.invalidated", {"resource": resource}, event_session)

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
            if not self._closed:
                self._on_reader_loop_exit(e)
            return
        # Clean close: the `async for` ends WITHOUT raising when the peer closes
        # the socket normally (websockets yields ConnectionClosedOK, which the
        # async iterator swallows to stop iteration — e.g. a graceful OpenClaw
        # restart sends a 1000/1001 close frame). The `except Exception` branch
        # never runs, so without this the pending RPCs hang the full timeout,
        # the DDB liveness row stays "connected" for its whole freshness window,
        # and no GATEWAY_DOWN reaches the UI. Route the normal exit through the
        # same teardown as the error path.
        if not self._closed:
            self._on_reader_loop_exit(_GatewayReaderClosed("gateway closed the connection"))

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
                getattr(self, "_room_run_context", {}).clear()
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
        # Cancel in-flight analytics emits (best-effort by contract) BEFORE the
        # drains below, so a slow Paperclip enrichment never competes with the
        # billing/push writes for the bounded close budget. An emit lost here is
        # the same accepted loss class as a run terminating while the backend
        # is down.
        for task in list(self._analytics_tasks):
            task.cancel()
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
        # Pending run-completion observers are left REGISTERED through the
        # drains below: the reader loop is still alive until it is cancelled
        # further down, so a genuine terminal that lands during the billing/
        # push/RPC drains must still be able to fire its observer — see the
        # handoff after the reader is cancelled.
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
        getattr(self, "_room_run_context", {}).clear()
        self._parked = False


class GatewayConnectionPool:
    """Pool of persistent gateway connections, one per active user."""

    # Set by close_all; ensure_connection refuses new dials once it flips so
    # a mid-shutdown dial can't insert past the shutdown snapshot. A class
    # default (not __init__ state) so the __new__-constructed test harnesses
    # inherit it without each one having to know the attribute exists.
    _closing: bool = False

    def __init__(self, conn_svc: Optional[ConnectionService] = None) -> None:
        self._conn_svc = conn_svc or ConnectionService()
        self._connections: Dict[str, GatewayConnection] = {}
        self._lock = asyncio.Lock()
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
        # Detached connections being drained+closed off the pool lock (the
        # changed-IP recreate path). Strong refs kept so the fire-and-forget
        # close task isn't GC'd mid-drain.
        self._background_close_tasks: set[asyncio.Task] = set()
        # Per-owner {endpoint: monotonic_abandoned_at} history of connections we
        # recreated AWAY from. Fences the changed-IP recreate against a STALE
        # resolve (a concurrent request straddling a task replacement) rolling a
        # newer connection back onto the dead task it replaced. A *history*, not a
        # single slot: across two fast replacements (A→B→C) a resolution still
        # carrying A must stay fenced even after B abandoned A's slot, else the
        # A-carrying caller would detach the healthy C and dial dead A — and,
        # worse, record C as superseded, wedging every later resolve of the real
        # live task until the record lapses. Bounded to the last
        # `_SUPERSEDED_HISTORY_MAX` endpoints and TTL-swept per entry.
        self._superseded_ip: Dict[str, Dict[str, float]] = {}

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
        event_kind: str | None = None,
        thread_root_message_id: str | None = None,
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
        conn.mark_run_active(
            session_key,
            agent_id,
            member_id=member_id,
            event_kind=event_kind,
            thread_root_message_id=thread_root_message_id,
        )

    async def subscribe_room_session(self, user_id: str, session_key: str) -> None:
        """Stream a room session's run events to the owner's connection.

        No-ops without a connection, like ``mark_run_active``. See
        ``GatewayConnection.subscribe_room_session`` for why a room run emits
        nothing to us until this lands.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        await conn.subscribe_room_session(session_key)

    def clear_active_run(self, user_id: str, session_key: str, *, preserve_room_context: bool = False) -> None:
        """Tear down the active-run window opened by ``mark_run_active``.

        The dispatcher marks a run active *before* issuing chat.send so the
        parked-vs-dead window is open for the whole dispatch. If that chat.send
        never reaches OpenClaw (timeout, container unreachable, gateway error),
        no terminal event will ever arrive to clear the window — leaving a stale
        entry that keeps the connection parking on a WS drop forever. The
        dispatcher calls this on the chat.send failure path. When delivery is
        uncertain, ``preserve_room_context`` retains only bounded routing
        metadata for frames that may still arrive; it does not keep the run
        active. Silently no-ops when no connection exists. Idempotent.
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn._clear_active_run(session_key, preserve_room_context=preserve_room_context)

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
            # Under the cap and inside the window: ADMIT. A trial owner's
            # spend is governed by the $3/day meter cap, never by credits, so
            # Layer 3 has no jurisdiction over them and this must not fall
            # through to it.
            #
            # Falling through was survivable only while the credits layer
            # failed OPEN on a missing ledger. It is about to fail CLOSED, and
            # a trial owner can very easily carry provider_choice ==
            # "bedrock_claude": /billing/trial-checkout writes it
            # SYNCHRONOUSLY before redirecting to Stripe, so anyone who opens
            # Checkout and closes the tab keeps it while staying trial_local.
            # Trial owners are deliberately never given a Metronome ledger
            # (ensure_metronome_customer short-circuits them), so Layer 3
            # would find no ledger and block them out of a fully-funded free
            # tier — permanently, since the midnight cap reset does not clear
            # a credits verdict. `provision_gate` already returns here rather
            # than falling through; this makes the chat gate agree.
            return {"blocked": False}

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
        """Create and connect one owner-bound machine gateway connection."""
        conn = GatewayConnection(
            user_id=user_id,
            ip=ip,
            token=token,
            owner_id=user_id,
            conn_svc=self._conn_svc,
        )
        await conn.connect()
        self._connections[user_id] = conn
        return conn

    def _close_detached(self, conn: GatewayConnection) -> None:
        """Drain + close a connection already removed from the pool, OFF the lock.

        ``close()`` spends up to ~25s draining billing/push/completion tasks +
        pending RPCs + the ws-close timeout; holding the pool-wide ``_lock``
        across that head-of-line-blocks ``ensure_connection`` for every other
        owner. The changed-IP recreate path detaches the dead connection under
        the lock and hands its teardown here so it finishes in the background.
        """

        async def _run() -> None:
            try:
                await conn.close()
            except Exception:
                logger.exception("Background close failed for user %s", conn.user_id)
            finally:
                # close() SCHEDULES the terminal liveness delete up front (line
                # ~2456, before the ws teardown that can raise), then this task
                # removes itself from `_background_close_tasks` the moment it ends —
                # so a task finishing before shutdown is already gone from the set
                # close_all() drains, and its queued delete would be orphaned and
                # cancelled by lifespan teardown, leaving the detached (dead)
                # connection's row falsely "ready". Drain in `finally` so the write
                # lands even when close() raised AFTER scheduling it.
                try:
                    await conn.drain_liveness_write()
                except Exception:
                    logger.exception("Background liveness drain failed for user %s", conn.user_id)

        task = asyncio.create_task(_run())
        self._background_close_tasks.add(task)
        task.add_done_callback(self._background_close_tasks.discard)

    async def send_rpc(
        self,
        user_id: str,
        req_id: str,
        method: str,
        params: dict,
        ip: str,
        token: str,
        timeout: float | None = None,
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

        ``timeout`` — optional override for how long to wait for the RPC
        response, forwarded to ``GatewayConnection.wait_for_response``.
        Default ``None`` preserves that method's own ``_RPC_TIMEOUT``
        default for every existing caller; pass explicitly when a
        particular RPC is known to run long under contention (e.g. the
        pairing ``agents.create``/``agents.update`` calls).
        """
        conn = await self.ensure_connection(user_id, ip, token)
        # Trust boundary: `req_id` is client-chosen (the caller echoes it back to
        # that same client verbatim) and is NOT unique across the tabs /
        # org-members sharing this one per-owner connection — two callers can
        # pick the same id, and routers/channels.py even ships a constant one.
        # The pending-futures map (`_pending_rpcs`) is per-owner, so a duplicate
        # id silently overwrites one caller's future and crosses their responses.
        # Mint a fresh wire id per call so the on-the-wire id (hence the
        # `_pending_rpcs` key) can never collide across callers; the caller still
        # echoes its own req_id to its client independently, so this is invisible
        # downstream.
        wire_id = f"{req_id}-{uuid.uuid4().hex}"
        await conn.send_rpc(wire_id, method, params)
        try:
            if timeout is not None:
                return await conn.wait_for_response(wire_id, timeout=timeout)
            return await conn.wait_for_response(wire_id)
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
            # Shutdown fence: a dial that would insert AFTER close_all's
            # snapshot would leave a live reader and a fresh gateway-liveness
            # row behind a dead process — other workers would read this
            # owner as \"ready\" for the whole 180s freshness window.
            # close_all sets the flag and then passes through this lock
            # once, so every dial that began before shutdown has either
            # inserted (and is closed by the snapshot) or refuses here.
            if self._closing:
                raise GatewayReconnectThrottled("pool is shutting down")
            conn = self._connections.get(user_id)
            # A CHANGED task IP is proof of ECS task replacement — task IPs are
            # ephemeral (every deploy / OOM / health-restart / reprovision hands
            # out a new one). The prior connection can still report is_connected
            # (the local websocket state flag reads OPEN for up to the ping
            # timeout because the old task's ENI vanished with no FIN), so
            # *reusing* it would time out every RPC against a dead address — the
            # prod outage this guards. Merely adopting `conn.ip = ip` does NOT
            # help when the socket still looks OPEN and neither the parked nor
            # the stale branch below fires; the connection is handed back as-is
            # and every RPC on it dies. Tear it down and recreate against the
            # fresh endpoint. (Gated by the reconnect floor like the other repair
            # paths, so a discover_ip that flaps during a rolling-deploy overlap
            # can't thrash the socket.)
            # Tracks whether a branch below already consulted the reconnect floor,
            # so the shared create path doesn't double-check it (which would
            # throttle the very reconnect that just passed) — yet a create that
            # runs WITHOUT a preceding branch (an absent owner) still gets gated.
            floor_checked = False
            if conn is not None and conn.ip != ip:
                if self._is_superseded_endpoint(user_id, ip):
                    # `ip` is an endpoint we recreated AWAY from within the race
                    # window — a STALE resolution that predates the newer
                    # connection now installed (concurrent callers straddling a
                    # task replacement, made worse because the dead task's health
                    # probe can burn a full 5s). Honoring it would tear down the
                    # newer HEALTHY connection and dial the dead task, recreating
                    # the very outage this recreate exists to close. Pin to the
                    # live endpoint so no path below dials the dead task.
                    logger.info(
                        "Ignoring stale endpoint %s for user=%s — superseded by live %s",
                        ip,
                        user_id,
                        conn.ip,
                    )
                    put_metric("gateway.connection", dimensions={"event": "stale_endpoint_ignored"})
                    ip = conn.ip
                else:
                    logger.info(
                        "Gateway endpoint changed for user=%s %s -> %s — recreating connection",
                        user_id,
                        conn.ip,
                        ip,
                    )
                    self._respect_reconnect_floor(user_id)
                    floor_checked = True
                    # Stamp the stale-endpoint fence at DETACH, before the dial —
                    # not after a successful install. If _create_connection raises,
                    # the owner is left ABSENT with no live connection; a fence
                    # stamped only on success would fail OPEN, letting a concurrent
                    # caller that already resolved this dead IP recreate straight
                    # onto it. Stamping here closes that window. Appended to a
                    # bounded history (not overwritten) so a resolution straddling a
                    # later replacement stays fenced too. The clock is the recycling
                    # backstop, not the race timer (see _SUPERSEDED_IP_TTL_S).
                    self._record_superseded_endpoint(user_id, conn.ip)
                    # Detach under the lock; drain + close OFF the lock so this
                    # owner's dead-socket teardown (~25s worst case) can't
                    # head-of-line-block ensure_connection for every other owner.
                    self._connections.pop(user_id, None)
                    self._close_detached(conn)
                    conn = None
            if conn is not None and conn._parked:
                self._respect_auth_backoff(user_id)
                self._respect_reconnect_floor(user_id)
                floor_checked = True
                try:
                    await conn.reopen()
                except GatewayConnectAuthError as exc:
                    self._record_auth_failure(user_id, exc.retry_after_ms)
                    raise
                self._clear_auth_backoff(user_id)
            elif conn is not None and not conn.is_connected:
                self._respect_reconnect_floor(user_id)
                floor_checked = True
                await conn.close()
                conn = None
            if conn is None:
                # A stale caller can reach the absent path bearing a SUPERSEDED IP:
                # a preceding recreate detached the old connection and its dial then
                # raised (owner now absent), or the caller was queued behind that
                # sequence. Its IP is the dead endpoint we just abandoned — there is
                # no live connection to pin it to (the changed-ip branch above only
                # fires when one exists), so dialing it would recreate the outage.
                # Refuse; the caller's reconnect loop re-resolves the current task.
                if self._is_superseded_endpoint(user_id, ip):
                    logger.info(
                        "Refusing superseded endpoint %s for absent user=%s — re-resolve",
                        ip,
                        user_id,
                    )
                    put_metric("gateway.connection", dimensions={"event": "stale_endpoint_refused"})
                    raise GatewayReconnectThrottled("stale endpoint; re-resolve")
                self._respect_auth_backoff(user_id)
                # A detach above (or a prior repair whose own re-create failed)
                # leaves the owner ABSENT, so a queued caller reaches this path
                # with no preceding branch. Without the floor here, repeated fast
                # connect failures (connection refused) would storm the container
                # at the frontend's sub-second reconnect cadence. Gate on the
                # floor unless a branch already did — but only when this owner has
                # reconnected before (a floor stamp exists), so a genuine
                # first-ever connection is never made to wait.
                if not floor_checked and user_id in self._last_reconnect_at:
                    self._respect_reconnect_floor(user_id)
                try:
                    conn = await self._create_connection(user_id, ip, token)
                except GatewayConnectAuthError as exc:
                    self._record_auth_failure(user_id, exc.retry_after_ms)
                    raise
                self._clear_auth_backoff(user_id)
            return conn

    def _record_superseded_endpoint(self, user_id: str, ip: str) -> None:
        """Append *ip* to this owner's superseded-endpoint history (bounded).

        A history, not a single slot, so consecutive replacements (A→B→C) don't
        drop A while a resolution still carrying A is in flight — dropping it lets
        that caller detach the healthy C and wedge the owner (see the field
        comment). Evicts the oldest entry past ``_SUPERSEDED_HISTORY_MAX``.
        """
        hist = self._superseded_ip.setdefault(user_id, {})
        hist[ip] = time.monotonic()
        if len(hist) > _SUPERSEDED_HISTORY_MAX:
            oldest = min(hist, key=hist.__getitem__)
            del hist[oldest]

    def _is_superseded_endpoint(self, user_id: str, ip: str) -> bool:
        """True if *ip* is an endpoint we recreated a connection AWAY from and
        still within the recycling backstop — a stale resolution that must not
        roll a newer connection back onto the dead task, nor be dialed on the
        absent path. The history holds every recently-abandoned endpoint (not just
        the latest), so it cannot be overwritten out from under a straddling
        caller. The ``_SUPERSEDED_IP_TTL_S`` sweep is only the IP-recycling
        backstop: an entry older than the window lapses so a genuinely recycled IP
        is trusted again.
        """
        hist = self._superseded_ip.get(user_id)
        if not hist:
            return False
        when = hist.get(ip)
        if when is None:
            return False
        if time.monotonic() - when >= _SUPERSEDED_IP_TTL_S:
            del hist[ip]
            if not hist:
                self._superseded_ip.pop(user_id, None)
            return False
        return True

    def _respect_reconnect_floor(self, user_id: str) -> None:
        """Raise if another reconnect for *user_id* fired within `_RECONNECT_FLOOR_S`.

        Keeps a misbehaving container from burning the open-handshake
        budget at sub-second cadence when the frontend's exponential
        reconnect collides with multiple in-flight retries server-side.

        The stamp is taken only on the pass-through path — a throttled
        attempt does NOT extend the floor, so a caller waiting out
        ``_RECONNECT_FLOOR_S`` always gets through on its next try. (An
        earlier version of this docstring claimed the stamp was sticky
        across raises; the code never did that, and designing a retry
        policy around the claim would over-back-off.)
        """
        now = time.monotonic()
        last = self._last_reconnect_at.get(user_id, 0.0)
        if last and (now - last) < _RECONNECT_FLOOR_S:
            put_metric("gateway.connection", dimensions={"event": "reconnect_throttled"})
            raise GatewayReconnectThrottled(
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
            raise GatewayAuthBackoff(
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

    def resolve_run(self, owner_id: str, run_id: str) -> dict | None:
        conn = self._connections.get(owner_id)
        if conn is None:
            return None
        for session_key, run in conn._active_runs.items():
            if run.get("run_id") == run_id:
                return {
                    "sessionKey": session_key,
                    "agentId": run.get("agent_id"),
                    "terminal": False,
                }
        terminal_session_key = conn._terminal_run_ids.get(run_id)
        if terminal_session_key is not None:
            return {
                "sessionKey": terminal_session_key,
                "terminal": True,
            }
        return None

    async def close_idle_user(self, owner_id: str) -> bool:
        """Close an idle machine connection before a durable trial stop.

        The pool lock serializes this decision with connection creation; active
        runs and parked connections are never detached.
        """
        async with self._lock:
            conn = self._connections.get(owner_id)
            if conn is None:
                return True
            if conn.has_active_runs or conn._parked:
                return False
            self._connections.pop(owner_id, None)
        await conn.close()
        await conn.drain_liveness_write()
        return True

    async def close_user(self, user_id: str) -> None:
        """Close gateway connection for a user."""
        conn = self._connections.pop(user_id, None)
        if conn:
            put_metric("gateway.connection", dimensions={"event": "disconnect"})
            await conn.close()
            # close() only SCHEDULES the terminal liveness delete; land it
            # before returning so every close path — grace task, shutdown
            # snapshot, admin teardown — leaves the row truthful the moment
            # it completes. Without this, a close finishing just before
            # lifespan teardown could have its queued delete cancelled,
            # leaving a dead worker's row falsely "ready" for the
            # freshness window.
            await conn.drain_liveness_write()
        self._last_reconnect_at.pop(user_id, None)
        # Same rationale as the reconnect-floor pop above: a torn-down user
        # must not leave auth-backoff state behind for a future incarnation
        # (a reprovision mints a fresh token, so a stale hold would suppress
        # its first legitimate connect).
        self._auth_backoff_until.pop(user_id, None)
        self._auth_failures.pop(user_id, None)

    async def close_all(self) -> None:
        """Fence new dials, close machine connections, and drain liveness."""
        self._closing = True
        async with self._lock:
            pass
        conns = list(self._connections.values())
        await asyncio.gather(
            *(self.close_user(owner_id) for owner_id in list(self._connections)),
            return_exceptions=True,
        )
        await asyncio.gather(*(conn.drain_liveness_write() for conn in conns), return_exceptions=True)
        active_closes = [task for task in self._background_close_tasks if not task.done()]
        if active_closes:
            await asyncio.wait(active_closes, timeout=5.0)

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


def _is_session_run_active(payload: dict) -> bool:
    """Resolve a session's run-active state from a `sessions.changed` payload.

    Port of OpenClaw's own client-side rule (openclaw/ui/src/lib/session-run-state.ts
    `isSessionRunActive`). The payload carries TWO run-state fields and they are not
    redundant — trusting only `hasActiveRun` is what stranded the chat status pill on
    "Working…" after a finished turn:

    - ``hasActiveRun`` is a liveness HEURISTIC: an OR over three in-memory registries
      (chat abort-controllers, projected remote runs, the embedded-agent run registry —
      see gateway/server-methods/session-active-runs.ts `resolveVisibleActiveSessionRunState`).
      Any one of them leaking an entry pins it true indefinitely, which is what happens
      to a container under load.
    - ``status`` is the DURABLE record, committed atomically with the transcript turn in
      the SQLite write transaction. It is set to "running" at run start and to a terminal
      value ("done"/"failed"/"killed"/"timeout") at run end
      (gateway/session-lifecycle-state.ts `deriveGatewaySessionLifecycleSnapshot`).

    So a terminal ``status`` WINS over a stale ``hasActiveRun: true``. Crucially this does
    NOT blank the status bar during long silent tool windows: `status` stays "running"
    across the yielded/waiting phase too (`isAgentLifecycleYieldedWaiting`), which is
    exactly the case the frontend's server-truth `active` flag exists to cover.

    Every other OpenClaw client already applies this rule; we are the outlier.
    """
    status = payload.get("status")
    if status and status != "running":
        return False
    has_active_run = payload.get("hasActiveRun")
    if isinstance(has_active_run, bool):
        return has_active_run
    return status == "running"


def parse_lightbulb_session_key(session_key: str) -> dict[str, str] | None:
    """Parse exactly ``agent:<uuid>:lightbulb:channel:<room-uuid>``."""
    parts = session_key.split(":") if isinstance(session_key, str) else []
    if len(parts) != 5 or parts[0] != "agent" or parts[2:4] != ["lightbulb", "channel"]:
        return None
    try:
        agent_id = str(uuid.UUID(parts[1]))
        room_id = str(uuid.UUID(parts[4]))
    except ValueError:
        return None
    if agent_id != parts[1].lower() or room_id != parts[4].lower():
        return None
    return {"agent_id": agent_id, "room_id": room_id}


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
