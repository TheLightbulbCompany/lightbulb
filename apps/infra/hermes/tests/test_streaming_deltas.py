"""Task 11: per-token `chunk`/`thinking` deltas, batched like the OpenClaw plugin's appsync.ts.

The fake sink here records one entry PER PUBLISH CALL (a list of frames), not one per frame --
the whole point of the batch is that five deltas cost one POST, and a per-frame sink cannot
tell the two apart.
"""
import threading

import pytest

from bridge import turn

ROOM, RUN = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Freeze the batch clock. Every test here asserts an exact batch shape, and >100 ms of
    real scheduler noise between two delta() calls would otherwise trip the age flush and
    split a batch (M1). The age test advances this list itself."""
    now = [1000.0]
    monkeypatch.setattr(turn.time, "monotonic", lambda: now[0])
    return now


def publisher(fail=False):
    """calls[i] == the frames carried by the i-th publish call."""
    calls = []

    def publish(endpoint, channel, *events):
        calls.append([e["payload"]["frame"] for e in events])
        if fail and any(f.get("append") for f in calls[-1]):  # only the streaming batches blow up
            raise RuntimeError("appsync said no")
        return 200

    return turn.FramePublisher("https://e.invalid", "/ws/c", ROOM, "ceo", RUN, None, publish=publish), calls


def test_two_deltas_ride_one_publish_as_append_chunks():
    p, calls = publisher()
    p.delta("Hel")
    p.delta("lo")
    assert calls == []  # still queued: no POST per token
    p.delta(None)
    assert calls == [[{"type": "chunk", "content": "Hel", "append": True, "runId": RUN, "agentId": "ceo"},
                      {"type": "chunk", "content": "lo", "append": True, "runId": RUN, "agentId": "ceo"}]]


def test_a_none_delta_on_an_empty_queue_publishes_nothing():
    p, calls = publisher()
    p.delta(None)
    p.delta("")
    assert calls == []


def test_a_full_batch_is_one_publish_and_the_sixth_delta_starts_the_next():
    p, calls = publisher()
    for i in range(5):
        p.delta(f"d{i}")
    assert len(calls) == 1 and len(calls[0]) == turn.FRAME_BATCH
    p.delta("d5")
    assert len(calls) == 1  # queued, not sent
    p.delta(None)
    assert len(calls) == 2 and [f["content"] for f in calls[1]] == ["d5"]


def test_a_delta_arriving_after_the_flush_window_ships_the_batch(clock):
    p, calls = publisher()
    p.delta("a")
    clock[0] += turn.FRAME_FLUSH_MS / 1000
    p.delta("b")
    assert len(calls) == 1 and [f["content"] for f in calls[0]] == ["a", "b"]


def test_queued_deltas_are_published_before_the_next_lifecycle_frame():
    p, calls = publisher()
    p.delta("thinking out loud")
    p.tool_start("call_1", "terminal", {"command": "ls"})
    assert [[f["type"] for f in call] for call in calls] == [["chunk"], ["tool_start"]]


def test_finished_flushes_the_queue_then_replaces_with_the_full_text():
    p, calls = publisher()
    p.delta("Hel")
    p.delta("lo")
    p.finished("completed", "Hello", None)
    assert [[f["type"] for f in call] for call in calls] == [
        ["chunk", "chunk"], ["chunk"], ["done"], ["run_ended"], ["run_active"]]
    assert [f["content"] for f in calls[0]] == ["Hel", "lo"]
    # the resync chunk is replace-semantics: no `append` key at all
    assert calls[1][0] == {"type": "chunk", "content": "Hello", "runId": RUN, "agentId": "ceo"}
    assert p._queue == []


def test_every_delta_is_published_verbatim_including_repeats():
    """Ruling M6-R49: no whole-text de-dupe. On the pinned tag nothing echoes the reply after
    streaming it (see the A2 note in _construct_agent), and a de-dupe keyed on the accumulated
    text also matches a run whose second delta repeats its first ('** ', '** ')."""
    p, calls = publisher()
    p.delta("** ")
    p.delta("** ")
    p.delta("Hello world")   # equals the accumulated text only by coincidence; still appended
    p.delta(None)
    assert [f["content"] for call in calls for f in call] == ["** ", "** ", "Hello world"]


def test_a_flush_never_publishes_more_than_the_appsync_cap():
    """I1: the batch slice is taken under the lock, so a queue that grew past FRAME_BATCH
    (a second streaming writer appending during a publish) splits into capped publishes
    instead of one oversize POST -- AppSync rejects that outright and loses ALL of it."""
    p, calls = publisher()
    for i in range(12):
        p._queue.append({"type": "chunk", "content": f"d{i}", "append": True})
    p._flush()
    assert [len(call) for call in calls] == [5, 5, 2]
    assert p._queue == []


def test_concurrent_writers_never_exceed_the_cap_and_never_lose_a_delta():
    p, calls = publisher()

    def stream(tag):
        for i in range(50):
            p.delta(f"{tag}{i}")

    threads = [threading.Thread(target=stream, args=(tag,)) for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    p.delta(None)
    assert max(len(call) for call in calls) <= turn.FRAME_BATCH
    assert sum(len(call) for call in calls) == 100


def test_a_failed_streaming_batch_is_swallowed_and_lifecycle_still_publishes():
    p, calls = publisher(fail=True)
    p.delta("Hel")
    p.delta("lo")
    p.finished("completed", "Hello", None)  # must not raise
    assert [[f["type"] for f in call] for call in calls] == [
        ["chunk", "chunk"], ["chunk"], ["done"], ["run_ended"], ["run_active"]]


def test_reasoning_deltas_become_append_thinking_frames():
    p, calls = publisher()
    p.thinking("step ")
    p.thinking("one")
    p.thinking(None)
    assert calls == [[{"type": "thinking", "content": "step ", "append": True, "runId": RUN, "agentId": "ceo"},
                      {"type": "thinking", "content": "one", "append": True, "runId": RUN, "agentId": "ceo"}]]


def test_the_stream_callbacks_are_wired_onto_the_agent(monkeypatch):
    """The whole feature is inert unless _construct_agent hands Hermes these two kwargs."""
    seen = {}
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda agent_id: None)
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id, model: {})

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    import sys
    import types
    fake = types.ModuleType("run_agent")
    fake.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    p, _calls = publisher()
    turn._construct_agent("ceo", {"room": {"id": ROOM, "kind": "channel"}}, p, "sess")
    assert seen["stream_delta_callback"] == p.delta
    assert seen["reasoning_callback"] == p.thinking
    assert "thinking_callback" not in seen  # a spinner status string, not the reasoning stream


def test_a_publisher_with_no_channel_queues_nothing():
    p = turn.FramePublisher("", "", ROOM, "ceo", RUN, None,
                            publish=lambda *a: (_ for _ in ()).throw(AssertionError("no publish")))
    p.delta("x")
    p.thinking("y")
    p.finished("completed", "x", None)
    assert p._queue == []
