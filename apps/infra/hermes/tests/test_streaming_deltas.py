"""Task 11: per-token `chunk`/`thinking` deltas, batched like the OpenClaw plugin's appsync.ts.

The fake sink here records one entry PER PUBLISH CALL (a list of frames), not one per frame --
the whole point of the batch is that five deltas cost one POST, and a per-frame sink cannot
tell the two apart.
"""
from bridge import turn

ROOM, RUN = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


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


def test_a_delta_arriving_after_the_flush_window_ships_the_batch(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(turn.time, "monotonic", lambda: clock[0])
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


def test_a_whole_reply_echo_after_real_deltas_is_dropped():
    """A2: on the pinned tag (v2026.9.14 == 0.21.3) the ONLY full-response echo is
    turn_tool_round.py:170-175's guardrail halt, whose text is new prose -- so this guard
    is inert there and exists for a provider path that replays what it already streamed."""
    p, calls = publisher()
    p.delta("Hello ")
    p.delta("world")
    p.delta("Hello world")   # the echo
    p.delta("!")             # a real delta after it still appends
    p.delta(None)
    assert [f["content"] for call in calls for f in call] == ["Hello ", "world", "!"]


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
