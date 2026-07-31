# backend/tests/unit/core/test_frame_sender.py
"""Unit tests for core.gateway.frame_sender.FrameSender.

Covers per-connection ordering, append-delta shaping (first frame per
(runId, type) ships full text; later frames ship deltas), failure-driven
resync, Gone handling, backlog coalescing, and overflow dropping.
"""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from core.gateway.frame_sender import FrameSender


def _chunk(delta: str, full: str, run="run-1"):
    return {"type": "chunk", "content": delta, "append": True, "_full_content": full, "runId": run}


class RecordingApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # Mirrors ManagementApiClient.gone_confirmed. Defaults to confirmed so
        # Gone-path tests exercise the prune; blip tests flip it to False.
        self.gone_confirmed_result = True

    def send_message(self, conn_id, payload):
        self.calls.append((conn_id, payload))
        return True

    def gone_confirmed(self, conn_id):
        return self.gone_confirmed_result


class BlockingApi(RecordingApi):
    """send_message blocks on a gate so tests can pile frames into the queue
    while the worker is mid-send."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def send_message(self, conn_id, payload):
        self.calls.append((conn_id, payload))
        self.gate.wait(timeout=2)
        return True


async def _wait_for_calls(api, n, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while len(api.calls) < n:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {n} calls (got {len(api.calls)})")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_first_stream_frame_ships_full_then_deltas():
    api = RecordingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("He", "He"))
    await sender.flush()
    sender.enqueue("c1", _chunk("llo", "Hello"))
    await sender.flush()

    assert api.calls == [
        # First frame for (run-1, chunk): legacy full-text form, no append.
        ("c1", {"type": "chunk", "content": "He", "runId": "run-1"}),
        # Continuity established: delta form, no _full_content on the wire.
        ("c1", {"type": "chunk", "content": "llo", "append": True, "runId": "run-1"}),
    ]


@pytest.mark.asyncio
async def test_mid_run_connection_gets_full_text_resync():
    """A connection that first appears mid-run (reconnect / new tab) must
    receive the accumulated text, not just the latest delta."""
    api = RecordingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("He", "He"))
    await sender.flush()
    frame = _chunk("llo", "Hello")
    sender.enqueue("c1", frame)
    sender.enqueue("c2", frame)  # c2 joins mid-run — same shared frame dict
    await sender.flush()

    c1_frames = [p for c, p in api.calls if c == "c1"]
    c2_frames = [p for c, p in api.calls if c == "c2"]
    assert c1_frames[-1] == {"type": "chunk", "content": "llo", "append": True, "runId": "run-1"}
    assert c2_frames == [{"type": "chunk", "content": "Hello", "runId": "run-1"}]
    # The shared frame dict is never mutated by shaping.
    assert frame["_full_content"] == "Hello"


@pytest.mark.asyncio
async def test_new_run_restarts_full_form():
    api = RecordingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("Hi", "Hi", run="run-1"))
    await sender.flush()
    sender.enqueue("c1", _chunk("Yo", "Yo", run="run-2"))
    await sender.flush()

    assert api.calls[1] == ("c1", {"type": "chunk", "content": "Yo", "runId": "run-2"})


@pytest.mark.asyncio
async def test_chunk_and_thinking_keep_separate_continuity():
    api = RecordingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("a", "a"))
    await sender.flush()
    think = {"type": "thinking", "content": "t", "append": True, "_full_content": "t", "runId": "run-1"}
    sender.enqueue("c1", think)
    await sender.flush()
    sender.enqueue("c1", _chunk("b", "ab"))
    await sender.flush()

    # Thinking's first frame ships full; the interleaved thinking frame must
    # NOT clobber the chunk continuity — the later chunk stays a delta.
    # (Interleaved chunk/thinking is the normal shape of a thinking-enabled
    # run, so per-type continuity is what keeps deltas flowing at all.)
    assert api.calls[1][1] == {"type": "thinking", "content": "t", "runId": "run-1"}
    assert api.calls[2][1] == {"type": "chunk", "content": "b", "append": True, "runId": "run-1"}


@pytest.mark.asyncio
async def test_transient_failure_resyncs_to_full():
    api = RecordingApi()
    real = api.send_message
    fail_next = {"on": False}

    def flaky(conn_id, payload):
        if fail_next["on"]:
            fail_next["on"] = False
            raise RuntimeError("throttled")
        return real(conn_id, payload)

    api.send_message = flaky
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("He", "He"))
    await sender.flush()
    fail_next["on"] = True
    sender.enqueue("c1", _chunk("llo", "Hello"))  # raises in-flight, frame lost
    await sender.flush()
    sender.enqueue("c1", _chunk("!", "Hello!"))
    await sender.flush()

    # The frame after the failure re-ships the FULL text so the lost delta
    # ("llo") is recovered rather than leaving a hole.
    assert api.calls[-1] == ("c1", {"type": "chunk", "content": "Hello!", "runId": "run-1"})


@pytest.mark.asyncio
async def test_gone_drops_queue_and_calls_on_gone_once():
    api = RecordingApi()
    api.send_message = MagicMock(return_value=False)
    pruned: list[str] = []

    async def on_gone(conn_id):
        pruned.append(conn_id)

    sender = FrameSender(api)
    sender.enqueue("c1", {"type": "done", "runId": "r"}, on_gone=on_gone)
    sender.enqueue("c1", {"type": "run_ended", "runId": "r"}, on_gone=on_gone)
    await sender.flush()

    # First send observes Gone: remaining frames dropped, one prune call.
    assert api.send_message.call_count == 1
    assert pruned == ["c1"]


@pytest.mark.asyncio
async def test_unconfirmed_gone_skips_frame_without_pruning():
    """A spurious API Gateway 410 on a live connection must not prune: the
    frame is skipped (recovered by full-text resync on the next stream
    frame) and later frames keep flowing."""
    api = RecordingApi()
    api.gone_confirmed_result = False
    real = api.send_message
    gone_next = {"on": False}

    def flaky(conn_id, payload):
        if gone_next["on"]:
            gone_next["on"] = False
            real(conn_id, payload)
            return False
        return real(conn_id, payload)

    api.send_message = flaky
    pruned: list[str] = []

    async def on_gone(conn_id):
        pruned.append(conn_id)

    sender = FrameSender(api)
    sender.enqueue("c1", _chunk("He", "He"), on_gone=on_gone)
    await sender.flush()
    gone_next["on"] = True
    sender.enqueue("c1", _chunk("llo", "Hello"), on_gone=on_gone)  # 410 blip, frame lost
    await sender.flush()
    sender.enqueue("c1", _chunk("!", "Hello!"), on_gone=on_gone)
    await sender.flush()

    assert pruned == []
    # The frame after the blip re-ships the FULL text so the lost delta is
    # recovered, exactly like the transient-exception path.
    assert api.calls[-1] == ("c1", {"type": "chunk", "content": "Hello!", "runId": "run-1"})


@pytest.mark.asyncio
async def test_unconfirmed_gone_keeps_draining_queued_frames():
    api = RecordingApi()
    api.gone_confirmed_result = False
    api.send_message = MagicMock(return_value=False)
    pruned: list[str] = []

    async def on_gone(conn_id):
        pruned.append(conn_id)

    sender = FrameSender(api)
    sender.enqueue("c1", {"type": "done", "runId": "r"}, on_gone=on_gone)
    sender.enqueue("c1", {"type": "run_ended", "runId": "r"}, on_gone=on_gone)
    await sender.flush()

    # Unlike confirmed Gone, every queued frame is still attempted.
    assert api.send_message.call_count == 2
    assert pruned == []


@pytest.mark.asyncio
async def test_backlog_coalesces_append_frames():
    api = BlockingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("a", "a"))
    await _wait_for_calls(api, 1)  # worker is now blocked mid-send of frame 1
    sender.enqueue("c1", _chunk("b", "ab"))
    sender.enqueue("c1", _chunk("c", "abc"))
    api.gate.set()
    await sender.flush()

    assert api.calls[0][1] == {"type": "chunk", "content": "a", "runId": "run-1"}
    # Frames 2+3 merged into one delta while the worker was busy.
    assert api.calls[1][1] == {"type": "chunk", "content": "bc", "append": True, "runId": "run-1"}
    assert len(api.calls) == 2


@pytest.mark.asyncio
async def test_backlog_does_not_coalesce_across_runs():
    api = BlockingApi()
    sender = FrameSender(api)

    sender.enqueue("c1", _chunk("a", "a", run="run-1"))
    await _wait_for_calls(api, 1)
    sender.enqueue("c1", _chunk("b", "ab", run="run-1"))
    sender.enqueue("c1", _chunk("x", "x", run="run-2"))
    api.gate.set()
    await sender.flush()

    assert len(api.calls) == 3
    assert api.calls[1][1]["runId"] == "run-1"
    assert api.calls[2][1] == {"type": "chunk", "content": "x", "runId": "run-2"}


@pytest.mark.asyncio
async def test_overflow_drops_oldest_stream_frame_and_resyncs():
    api = BlockingApi()
    sender = FrameSender(api, max_queue=2)

    sender.enqueue("c1", _chunk("a", "a"))
    await _wait_for_calls(api, 1)  # worker blocked; queue is now empty
    sender.enqueue("c1", _chunk("b", "ab", run="run-1"))
    sender.enqueue("c1", {"type": "tool_start", "tool": "bash", "runId": "run-1"})
    # Queue is at max (2). A non-coalescible frame forces a drop of the
    # oldest stream frame (the "b" delta).
    sender.enqueue("c1", {"type": "done", "runId": "run-1"})
    api.gate.set()
    await sender.flush()
    # The dropped delta is recovered: the next stream frame ships full text.
    sender.enqueue("c1", _chunk("c", "abc"))
    await sender.flush()

    types = [p.get("type") for _, p in api.calls]
    assert types == ["chunk", "tool_start", "done", "chunk"]
    assert api.calls[-1][1] == {"type": "chunk", "content": "abc", "runId": "run-1"}


@pytest.mark.asyncio
async def test_non_stream_frames_pass_through_untouched():
    api = RecordingApi()
    sender = FrameSender(api)
    frame = {"type": "tool_start", "tool": "bash", "args": {"cmd": "ls"}, "runId": "r"}
    sender.enqueue("c1", frame)
    await sender.flush()
    assert api.calls == [("c1", frame)]


@pytest.mark.asyncio
async def test_forget_connection_drops_queued_frames():
    api = BlockingApi()
    sender = FrameSender(api)
    sender.enqueue("c1", _chunk("a", "a"))
    await _wait_for_calls(api, 1)  # worker is blocked mid-send of frame 1
    sender.enqueue("c1", _chunk("b", "ab"))
    sender.forget_connection("c1")
    api.gate.set()
    await sender.flush()
    # flush() does not track the now-detached worker, so give it real time to
    # (wrongly) drain the queued frame if forget_connection had left it in the
    # deque — the worker must exit right after its in-flight send instead.
    await asyncio.sleep(0.05)
    assert len(api.calls) == 1


@pytest.mark.asyncio
async def test_order_preserved_under_interleaved_enqueues():
    api = RecordingApi()
    sender = FrameSender(api)
    for i in range(20):
        sender.enqueue("c1", {"type": "seq", "i": i})
    await sender.flush()
    assert [p["i"] for _, p in api.calls] == list(range(20))
