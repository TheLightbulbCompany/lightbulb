"""End-to-end chat latency tracking, correlated by run id.

`chat.e2e.latency` is the platform's headline chat-latency SLO metric — alarm
W9 in ``apps/infra/lib/stacks/observability-stack.ts`` alarms on its p99 over a
5-minute window (threshold 20s). The span it measures:

    START  — a chat.send is dispatched to the user's OpenClaw container
              (``routers/websocket_chat.py::_process_agent_chat_background``,
              right after the chat.send RPC is acked).
    END    — the terminal ``chat`` event for the SAME run arrives on the
              gateway reader loop (``core/gateway/connection_pool.py``):
              ``final`` (success), ``error`` or ``aborted``.

START and END run in DIFFERENT async tasks (the dispatch task vs. the
per-connection reader loop), so the only correlation handle is the run id
(== the chat.send ``idempotencyKey`` == the ``runId`` OpenClaw stamps on every
event for that run). This module owns a process-global bounded map of
``run_id -> start_monotonic`` so the two tasks can rendezvous.

Bounding (CRITICAL — this is the chat hot path):
  - ``_MAX_PENDING`` (4096) caps the map size. Runs that never terminate
    (client disconnect, container death, errors never surfaced as a chat
    terminal) would otherwise leak one entry each, forever. When the cap is
    hit, the OLDEST entry is evicted (insertion-ordered ``OrderedDict`` FIFO).
  - ``_MAX_AGE_S`` (1800s / 30min) ages out stale starts on every record/take.
    Any realistic chat turn completes well inside 30 minutes; a pending entry
    older than that is presumed orphaned and dropped without emitting (a stale
    elapsed would pollute the p99).
  Net: the map can hold at most ``_MAX_PENDING`` entries, none older than
  ``_MAX_AGE_S`` (modulo the lazy sweep cadence), regardless of how many runs
  never complete.

Everything here is BEST-EFFORT and must NEVER raise into the chat/event path.
``record_start`` and ``record_end`` swallow + log all exceptions; the metric
emission is the last thing they do and is itself guarded.
"""

import asyncio
import logging
import time
from collections import OrderedDict

from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

_METRIC_NAME = "chat.e2e.latency"

# Hard cap on tracked-but-unterminated runs. Far above any realistic count of
# concurrent in-flight chat turns across the whole backend task; the FIFO
# eviction is a safety net for runs whose terminal frame never arrives.
_MAX_PENDING = 4096

# Maximum age of a pending start before it is presumed orphaned and dropped
# without emitting. 30min comfortably exceeds the longest realistic single
# chat turn (matches the 300s gateway ping timeout headroom many-fold).
_MAX_AGE_S = 1800.0


class RunLatencyTracker:
    """Bounded, async-safe map of ``run_id -> start_monotonic``.

    One process-global instance (``_TRACKER``) is shared by the chat dispatch
    task (which records the start) and the gateway reader loop (which records
    the terminal and emits the metric). Access is serialized by an ``asyncio``
    lock so the two tasks can't corrupt the OrderedDict.
    """

    def __init__(self, max_pending: int = _MAX_PENDING, max_age_s: float = _MAX_AGE_S) -> None:
        self._starts: "OrderedDict[str, float]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_pending = max_pending
        self._max_age_s = max_age_s

    async def record_start(self, run_id: str) -> None:
        """Stamp the START monotonic time for *run_id*.

        Best-effort: never raises. A blank/missing run_id is ignored.
        """
        if not run_id:
            return
        try:
            now = time.monotonic()
            async with self._lock:
                # Re-insert at the end so a duplicate start refreshes ordering.
                self._starts.pop(run_id, None)
                self._starts[run_id] = now
                self._sweep_locked(now)
                # FIFO size cap: evict oldest until within bound.
                while len(self._starts) > self._max_pending:
                    self._starts.popitem(last=False)
        except Exception:
            logger.warning("e2e latency record_start failed for run_id=%s", run_id, exc_info=True)

    async def record_end(self, run_id: str) -> None:
        """Compute elapsed ms for *run_id* and emit ``chat.e2e.latency``.

        No-op (silent) when there is no matching start — a terminal can arrive
        for a run we never tracked (cron/channel runs, a start that aged out, a
        terminal landing on a different backend task than the dispatch). Only a
        run with a real recorded start produces a metric, so the p99 reflects
        genuine round-trips. Best-effort: never raises into the event path.
        """
        if not run_id:
            return
        try:
            now = time.monotonic()
            async with self._lock:
                start = self._starts.pop(run_id, None)
                self._sweep_locked(now)
            if start is None:
                return
            elapsed_ms = max(0.0, (now - start) * 1000.0)
            # No extra dimensions: alarm W9 queries chat.e2e.latency on
            # {env, service} only (auto-injected by put_metric). A custom
            # dimension here would create a separate, un-alarmed metric stream.
            put_metric(_METRIC_NAME, value=elapsed_ms, unit="Milliseconds")
        except Exception:
            logger.warning("e2e latency record_end failed for run_id=%s", run_id, exc_info=True)

    def _sweep_locked(self, now: float) -> None:
        """Drop entries older than ``max_age_s``. Caller must hold the lock.

        Insertion-ordered, so the oldest entries are at the front: pop from the
        front until the head is within the age window.
        """
        cutoff = now - self._max_age_s
        while self._starts:
            oldest_run_id, oldest_start = next(iter(self._starts.items()))
            if oldest_start >= cutoff:
                break
            self._starts.popitem(last=False)

    def _pending_count(self) -> int:
        """Test/diagnostic helper — current number of tracked starts."""
        return len(self._starts)


# Process-global singleton shared across the dispatch + reader-loop tasks.
_TRACKER = RunLatencyTracker()


async def record_chat_start(run_id: str) -> None:
    """Record the e2e START for *run_id* (see module docstring)."""
    await _TRACKER.record_start(run_id)


async def record_chat_end(run_id: str) -> None:
    """Record the e2e END for *run_id* and emit ``chat.e2e.latency``."""
    await _TRACKER.record_end(run_id)
