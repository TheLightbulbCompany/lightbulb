# backend/core/gateway/frame_sender.py
"""Ordered, non-blocking delivery of frontend WebSocket frames.

``ManagementApiClient.send_message`` is synchronous boto3 — an HTTPS round
trip to the API Gateway Management API. Calling it inline from the gateway
reader loop blocks the entire asyncio event loop for the duration of the
POST, which serializes every user's stream behind every other user's chunk
delivery (the backend runs a single event loop per task). This module gives
each frontend connection its own FIFO queue drained by a worker task that
performs the blocking send in a thread:

- frame order per connection is preserved (single drainer per connection);
- the event loop never blocks on delivery;
- different connections deliver concurrently (bounded by the default
  ``asyncio.to_thread`` pool).

Append-delta shaping
--------------------

Streaming text frames (``chunk`` / ``thinking``) arrive from
``_transform_agent_event`` carrying BOTH the incremental delta
(``content`` + ``append: True``) and the full accumulated text
(``_full_content``, internal — never sent). The sender decides per
connection which shape to deliver:

- the FIRST stream frame for a given ``(runId, type)`` on a connection is
  sent as the full text with no ``append`` flag — byte-identical to the
  legacy cumulative-replacement contract. A tab that connects mid-run (or
  a connection that just dropped a frame) therefore self-heals on its next
  frame instead of missing the run's prefix forever;
- subsequent frames for the same ``(runId, type)`` are sent as append
  deltas — O(delta) bytes instead of re-shipping the whole message;
- any send failure (or a backlog drop) clears the continuity key, forcing
  the next stream frame back to the full form.

Backlog coalescing: when a stream frame is enqueued while the previous
stream frame for the same ``(runId, type)`` is still queued, the two merge
(deltas concatenated, full text replaced) instead of queuing separately.
Under backpressure the queue holds at most one pending stream frame per
``(runId, type)``, so a slow consumer converges instead of falling ever
further behind.

Scope of the ordering guarantee: FrameSender-routed frames only. The
router's fire-and-forget sends (``send_message_async``) bypass these
queues and may interleave arbitrarily with queued frames on the same
connection — acceptable because those frames (RPC responses matched by
req id, pong, gate errors) are order-independent by design. A new send
that DOES need ordering relative to the chat stream must go through
``enqueue``, not ``send_message_async``.
"""

import asyncio
import logging
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable, Optional

from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Frame types that participate in append-delta shaping/coalescing.
_STREAM_TYPES = frozenset({"chunk", "thinking"})

# Per-connection queue bound. With stream coalescing the queue holds at most
# one pending stream frame per (runId, type); everything else (tool pills,
# terminals, status changes) is low-rate, so a full queue means the consumer
# is effectively dead and about to be pruned as Gone anyway.
_MAX_QUEUE = 512

# Bound on the continuity-key map (connectionId -> last (runId, type) shipped).
# Entries are dropped on Gone; the LRU cap is only a safety net against
# connections that vanish without ever going Gone. Far above the realistic
# per-task connection count.
_STREAM_KEYS_CAP = 4096

# One queued frame: (frame, on_gone). `on_gone` is the async prune callback
# supplied at enqueue time; the drainer invokes the most recently supplied one
# when the connection turns out to be Gone.
_Entry = tuple[dict, Optional[Callable[[str], Awaitable[None]]]]


class _ConnQueue:
    __slots__ = ("frames", "worker")

    def __init__(self) -> None:
        self.frames: deque[_Entry] = deque()
        self.worker: Optional[asyncio.Task] = None


def _is_appendable(frame: dict) -> bool:
    """Whether *frame* is a stream frame in append form (delta + full text)."""
    return frame.get("type") in _STREAM_TYPES and frame.get("append") is True and "_full_content" in frame


class FrameSender:
    """Per-connection ordered async delivery to the Management API."""

    def __init__(self, management_api: Any, *, max_queue: int = _MAX_QUEUE) -> None:
        self._api = management_api
        self._max_queue = max_queue
        self._queues: dict[str, _ConnQueue] = {}
        # connectionId -> {frame type: runId} continuity map. Keyed per frame
        # TYPE so interleaved chunk/thinking frames (the normal shape of a
        # thinking-enabled run) each keep their own delta continuity instead
        # of forcing a full-text resync on every alternation. Survives queue
        # drain (queues retire whenever a connection goes idle between 150ms
        # lumps) so mid-run frames keep their delta form.
        self._stream_keys: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # -- enqueue -----------------------------------------------------------

    def enqueue(
        self,
        conn_id: str,
        frame: dict,
        on_gone: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Queue *frame* for ordered delivery to *conn_id* and return
        immediately. Never raises; delivery failures are handled by the
        drainer (transient → skip + resync, Gone → *on_gone*).

        *frame* may be shared across recipients — it is never mutated here
        (coalescing and shaping always build fresh dicts).
        """
        q = self._queues.get(conn_id)
        if q is None:
            q = _ConnQueue()
            self._queues[conn_id] = q

        if q.frames and _is_appendable(frame):
            tail_frame, _ = q.frames[-1]
            if (
                _is_appendable(tail_frame)
                and tail_frame.get("type") == frame.get("type")
                and tail_frame.get("runId") == frame.get("runId")
            ):
                merged = dict(tail_frame)
                merged["content"] = tail_frame["content"] + frame["content"]
                merged["_full_content"] = frame["_full_content"]
                q.frames[-1] = (merged, on_gone)
                self._spawn_if_needed(conn_id, q)
                return

        if len(q.frames) >= self._max_queue:
            self._drop_one(conn_id, q)
        q.frames.append((frame, on_gone))
        self._spawn_if_needed(conn_id, q)

    def _drop_one(self, conn_id: str, q: _ConnQueue) -> None:
        """Backlog overflow: drop the oldest stream frame if any (its text is
        recovered by the resync-to-full below), else the oldest frame outright.
        """
        for i, (frame, _) in enumerate(q.frames):
            if _is_appendable(frame):
                del q.frames[i]
                # Force the next stream frame to ship full text so the dropped
                # delta is recovered rather than leaving a hole in the message.
                self._stream_keys.pop(conn_id, None)
                put_metric("gateway.frontend.frame_dropped", dimensions={"kind": "stream"})
                logger.warning("Frame queue overflow for %s — dropped stream frame", conn_id)
                return
        dropped, _ = q.frames.popleft()
        put_metric(
            "gateway.frontend.frame_dropped",
            dimensions={"kind": str(dropped.get("type", "?"))},
        )
        logger.warning(
            "Frame queue overflow for %s — dropped %s frame",
            conn_id,
            dropped.get("type"),
        )

    def _spawn_if_needed(self, conn_id: str, q: _ConnQueue) -> None:
        if q.worker is None or q.worker.done():
            q.worker = asyncio.create_task(self._drain(conn_id, q))

    # -- drain -------------------------------------------------------------

    def _shape(self, conn_id: str, frame: dict) -> dict:
        """Resolve the wire shape for *frame* on *conn_id*.

        Non-stream frames pass through untouched. A stream frame ships as an
        append delta when this connection already received earlier frames of
        the same run for the same frame type, else as the legacy full-text
        form (which establishes continuity).
        """
        if "_full_content" not in frame:
            return frame
        frame_type = str(frame.get("type"))
        run_id = frame.get("runId")
        shaped = {k: v for k, v in frame.items() if k != "_full_content"}
        per_type = self._stream_keys.get(conn_id)
        if per_type is None:
            per_type = {}
            self._stream_keys[conn_id] = per_type
            while len(self._stream_keys) > _STREAM_KEYS_CAP:
                self._stream_keys.popitem(last=False)
        self._stream_keys.move_to_end(conn_id)
        if per_type.get(frame_type) != run_id:
            shaped["content"] = frame["_full_content"]
            shaped.pop("append", None)
            per_type[frame_type] = run_id
        return shaped

    async def _drain(self, conn_id: str, q: _ConnQueue) -> None:
        while q.frames:
            frame, on_gone = q.frames.popleft()
            payload = self._shape(conn_id, frame)
            try:
                ok = await asyncio.to_thread(self._api.send_message, conn_id, payload)
            except Exception:
                # Transient failure: skip the frame (matching the historical
                # inline behavior) but clear stream continuity so the next
                # stream frame re-ships the full text — an append stream must
                # never silently lose a delta.
                logger.warning("Transient forward failure to %s (not pruning)", conn_id)
                self._stream_keys.pop(conn_id, None)
                continue
            if not ok:
                # A lone GoneException can be a spurious API Gateway blip on a
                # live connection — prune only once the client confirms Gone
                # has persisted. Until then treat it like a transient failure:
                # skip the frame, resync on the next one, keep the connection.
                if not self._api.gone_confirmed(conn_id):
                    logger.warning("Unconfirmed Gone for %s — skipping frame (not pruning)", conn_id)
                    put_metric("gateway.frontend.gone_unconfirmed")
                    self._stream_keys.pop(conn_id, None)
                    continue
                # Confirmed Gone — drop remaining frames and hand the prune to
                # the most recent on_gone callback. No await between the pops
                # below and the queue check in enqueue, so no frame can slip
                # into the dict entry we discard.
                self._queues.pop(conn_id, None)
                self._stream_keys.pop(conn_id, None)
                if on_gone is not None:
                    try:
                        await on_gone(conn_id)
                    except Exception:
                        logger.warning("on_gone prune failed for %s", conn_id, exc_info=True)
                return
        # Queue drained. Retire the entry so idle connections don't leak;
        # both the emptiness check and the pop are synchronous, so an enqueue
        # can only observe the dict either with this (empty) queue or without
        # it — never a stranded frame.
        cur = self._queues.get(conn_id)
        if cur is q and not q.frames:
            self._queues.pop(conn_id, None)

    # -- lifecycle ---------------------------------------------------------

    def forget_connection(self, conn_id: str) -> None:
        """Drop queued frames + continuity state for *conn_id* (external prune)."""
        q = self._queues.pop(conn_id, None)
        if q is not None:
            # A worker blocked mid-send still holds this deque locally —
            # popping the dict entry alone would let it keep draining every
            # queued frame to a connection we already know is gone. Clearing
            # the deque makes it exit right after its in-flight send.
            q.frames.clear()
        self._stream_keys.pop(conn_id, None)

    async def flush(self) -> None:
        """Wait until every queued frame has been handed to the client.

        Shutdown/test helper — new enqueues issued while flushing are waited
        for too (the loop re-checks until no live workers remain).
        """
        while True:
            workers = [q.worker for q in self._queues.values() if q.worker and not q.worker.done()]
            if not workers:
                return
            await asyncio.gather(*workers, return_exceptions=True)
