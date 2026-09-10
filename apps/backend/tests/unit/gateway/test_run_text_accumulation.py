"""The full-text resync payload must span the RUN, not one assistant message.

OpenClaw clears its assistant text buffer at every ``message_start``
(``resetAssistantMessageState`` → ``state.deltaBuffer = ""``), and every tool
call ends one assistant message and starts another. So ``text`` on a
``stream: "assistant"`` agent event is cumulative within the current assistant
message only. Our chunk stream is keyed by ``runId`` and the frontend paints
one bubble per run, so a client that resyncs mid-run — a reconnect, a second
tab, a dropped Management-API POST — used to be handed the last text block
alone, dropping everything the agent said before the last tool call.

Verified against the fork at the pinned tag ``v2026.8.1-isol8.3``. Note this
is NOT an 8.1 regression: 7.2 resets at ``message_start`` identically. What
8.1 added is the gateway's own ``run.assistantScope`` prefix carrying, which
never engages on this path because the main assistant emit
(``buildAssistantStreamData`` in handlers.messages.update.ts /
handlers.messages.lifecycle.ts) passes no ``itemId``.
"""

import pytest

from core.gateway.connection_pool import GatewayConnection, _RUN_TEXT_CAP


def _conn() -> GatewayConnection:
    """An instance with only the state these paths touch."""
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._run_text = {}
    conn.user_id = "user_test"
    return conn


def _assistant(conn, text: str, delta: str, run_id: str = "run_1", **extra) -> dict:
    """Transform one assistant event and widen it, as _handle_message does."""
    data = {"text": text, "delta": delta, **extra}
    frame = conn._transform_agent_event({"stream": "assistant", "data": data, "runId": run_id})
    assert frame is not None
    conn._rewrite_run_full_text(frame)
    return frame


def _resync_text(frame: dict) -> str:
    """What FrameSender ships to a client with no continuity for this run."""
    return frame.get("_full_content", frame["content"])


# -- the core case: text -> tool_use -> text ----------------------------------


def test_resync_midrun_after_a_tool_call_reproduces_the_full_run_text():
    conn = _conn()
    # First assistant message.
    _assistant(conn, "Let me check", "Let me check")
    _assistant(conn, "Let me check the logs.", " the logs.")

    # Tool call. Upstream resets its buffer at the next message_start, so the
    # second message's `text` restarts from empty.
    conn._transform_agent_event(
        {"stream": "tool", "data": {"phase": "start", "name": "bash", "toolCallId": "t1"}, "runId": "run_1"}
    )

    # Second assistant message — `text` carries only this block.
    frame = _assistant(conn, "I found", "I found")
    assert frame["content"] == "I found"  # the append delta is untouched
    assert _resync_text(frame) == "Let me check the logs.I found"

    frame = _assistant(conn, "I found the bug.", " the bug.")
    assert _resync_text(frame) == "Let me check the logs.I found the bug."


def test_the_append_delta_path_is_unchanged():
    """A client that saw every frame must see byte-identical deltas."""
    conn = _conn()
    deltas = [
        _assistant(conn, "one", "one")["content"],
        _assistant(conn, "one two", " two")["content"],
        _assistant(conn, "three", "three")["content"],  # new assistant message
    ]
    assert deltas == ["one", " two", "three"]


def test_three_message_run_accumulates_every_block():
    conn = _conn()
    _assistant(conn, "A", "A")
    _assistant(conn, "B", "B")
    frame = _assistant(conn, "C", "C")
    assert _resync_text(frame) == "ABC"


def test_chunk_and_thinking_accumulate_independently():
    conn = _conn()
    _assistant(conn, "visible", "visible")
    think = conn._transform_agent_event(
        {"stream": "thinking", "data": {"text": "pondering", "delta": "pondering"}, "runId": "run_1"}
    )
    conn._rewrite_run_full_text(think)
    assert _resync_text(think) == "pondering"

    frame = _assistant(conn, "more", "more")
    assert _resync_text(frame) == "visiblemore"


def test_upstream_replace_frame_also_carries_the_run_prefix():
    """A cumulative-replacement frame replaces the client's whole buffer, so it
    must carry the run prefix too or it truncates exactly like the resync did."""
    conn = _conn()
    _assistant(conn, "before tool", "before tool")
    frame = _assistant(conn, "rewritten", "rewritten", replace=True)
    assert "append" not in frame
    assert frame["content"] == "before toolrewritten"


# -- isolation ----------------------------------------------------------------


def test_concurrent_runs_do_not_corrupt_each_other():
    """One GatewayConnection serves every member of a shared org container."""
    conn = _conn()
    _assistant(conn, "alice one", "alice one", run_id="run_a")
    _assistant(conn, "bob one", "bob one", run_id="run_b")
    a = _assistant(conn, "alice two", "alice two", run_id="run_a")
    b = _assistant(conn, "bob two", "bob two", run_id="run_b")
    assert _resync_text(a) == "alice onealice two"
    assert _resync_text(b) == "bob onebob two"


# -- cleanup ------------------------------------------------------------------


def test_terminal_state_releases_the_run_accumulator():
    """Cleanup lives at the chat terminal in _handle_message; assert the pop
    the handler performs actually frees the entry."""
    conn = _conn()
    _assistant(conn, "hello", "hello")
    assert "run_1" in conn._run_text
    conn._run_text.pop("run_1", None)
    assert conn._run_text == {}

    # A later run starts clean rather than inheriting the finished run's text.
    frame = _assistant(conn, "next run", "next run")
    assert _resync_text(frame) == "next run"


def test_accumulator_is_bounded_when_terminals_never_arrive():
    conn = _conn()
    for i in range(_RUN_TEXT_CAP + 20):
        _assistant(conn, "x", "x", run_id=f"run_{i}")
    assert len(conn._run_text) <= _RUN_TEXT_CAP


# -- seq gap (D7) -------------------------------------------------------------


def test_seq_gap_makes_the_next_text_frame_a_full_replace():
    conn = _conn()
    _assistant(conn, "seen", "seen")
    conn._run_text_state("run_1").resync = True

    frame = _assistant(conn, "seen and more", " and more")
    # Shipped as a replace, not a delta, so a client whose append stream has a
    # hole is repainted from whole-run authoritative text.
    assert "append" not in frame
    assert "_full_content" not in frame
    assert frame["content"] == "seen and more"

    # The flag is one-shot: the stream returns to deltas afterwards.
    nxt = _assistant(conn, "seen and more still", " still")
    assert nxt["append"] is True
    assert nxt["content"] == " still"


def test_seq_gap_heals_text_we_never_received():
    """The accumulator keeps upstream's per-message snapshot as its tail, so a
    delta lost between the container and us is recovered from the next event."""
    conn = _conn()
    _assistant(conn, "start", "start")
    conn._run_text_state("run_1").resync = True
    # " MIDDLE" never arrived; upstream's next `text` still carries it.
    frame = _assistant(conn, "start MIDDLE end", " end")
    assert frame["content"] == "start MIDDLE end"


# -- D7 wiring: the seq-gap event actually reaches the accumulator ------------


def _handle_message_conn():
    """A connection with just enough state for _handle_message's agent branch."""
    from unittest.mock import AsyncMock, MagicMock

    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "org_1"
    conn.owner_id = "org_1"
    conn._frontend_connections = set()
    conn._pending_rpcs = {}
    conn._management_api = MagicMock()
    conn._billing_tasks = set()
    conn._analytics_tasks = set()
    conn._conn_svc = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    conn._recipients_cache = None
    conn._recipients_cache_ts = 0.0
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._run_text = {}
    conn._parked = False
    conn._parked_watchdog_task = None
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()
    return conn


@pytest.mark.xfail(
    reason=(
        "SUSPECTED SOURCE BUG: the pre-cutover connection_pool.py wired "
        "`stream: 'error'` payloads with data.reason == 'seq gap' to "
        "`self._run_text_state(run_id).resync = True` (see git show "
        "187ef487d^:apps/backend/core/gateway/connection_pool.py around line "
        "2161). The native-messaging cutover (PR #2424, commit 187ef487d) "
        "rewrote `_handle_message`'s 'agent' event branch and this D7 wiring "
        "was dropped -- nothing in the current source ever sets "
        "`_RunText.resync = True`. Not patched per task scope -- reported "
        "instead."
    ),
    strict=True,
)
async def test_seq_gap_event_flags_the_run_through_handle_message():
    conn = _handle_message_conn()
    await conn._handle_message(
        {
            "type": "event",
            "event": "agent",
            "payload": {
                "runId": "run_1",
                "stream": "error",
                "sessionKey": "agent:friday:webchat:main",
                "data": {"reason": "seq gap", "expected": 2, "received": 5},
            },
        }
    )
    assert conn._run_text["run_1"].resync is True


async def test_a_non_gap_stream_error_does_not_flag_a_resync():
    conn = _handle_message_conn()
    await conn._handle_message(
        {
            "type": "event",
            "event": "agent",
            "payload": {
                "runId": "run_1",
                "stream": "error",
                "sessionKey": "agent:friday:webchat:main",
                "data": {"reason": "something else"},
            },
        }
    )
    assert conn._run_text == {}
