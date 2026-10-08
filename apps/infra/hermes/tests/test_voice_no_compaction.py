"""A spoken question is never stalled by context compaction (prod 10-07: four of five slow voice
turns were a 67-103 s compaction inside the turn; the fifth was one silent model call)."""
import json
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from bridge import server, sync, turn
from tests.test_voice_call_run import IDENTITY, REQUEST, ROOM, SESSION, _Agent, _closing, _fake_hermes, _frames, env  # noqa: F401


# ---- 1. no compaction inside the turn ------------------------------------------------------------
def test_a_spoken_turn_over_the_threshold_does_not_compact_inline(env, home):  # noqa: F811 -- the imported fixture
    """Hermes's own per-agent switch is off for the run, whatever the session's size."""
    turn.register_call_run(IDENTITY)
    assert turn._run_call(IDENTITY, REQUEST, _frames())[0] == "completed"
    assert env["agent"].compression_enabled is False


def test_room_turns_keep_hermes_compaction(home, monkeypatch):
    from tests import factories as f

    agent = _Agent(run_conversation=lambda **_kw: {"api_calls": 1, "final_response": "ok"}, compression_enabled=True)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    turn._run_hermes("ceo", f.ingress(), None)
    assert agent.compression_enabled is True


def _history(turns: int) -> list[dict]:
    rows = []
    for i in range(turns):
        rows += [{"role": "user", "content": f"q{i} " + "x" * 400},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]},
                 {"role": "tool", "tool_call_id": f"t{i}", "content": "y" * 400},
                 {"role": "assistant", "content": f"a{i}"}]
    return rows


def _rough(messages) -> int:
    return sum(len(json.dumps(m, default=str)) for m in messages) // 4


def test_a_spoken_turn_replays_a_bounded_tail_of_an_oversized_session(env, home, monkeypatch):  # noqa: F811 -- the imported fixture
    """The turn cannot compact, so a session past the bound is replayed from a later user turn
    instead of failing on a too-long request."""
    history = _history(40)
    monkeypatch.setattr(turn, "_room_history", lambda _agent: (list(history), None))
    monkeypatch.setattr(turn, "CALL_HISTORY_TOKENS", _rough(history) // 4)
    turn.register_call_run(IDENTITY)
    turn._run_call(IDENTITY, REQUEST, _frames())
    sent = env["convos"][0]["conversation_history"]
    assert 0 < len(sent) < len(history) and sent == history[-len(sent):]
    assert sent[0]["role"] == "user" and _rough(sent) <= turn.CALL_HISTORY_TOKENS


def test_a_session_under_the_bound_is_replayed_whole():
    history = _history(3)
    assert turn._bounded_history(history, 10**9) is history
    assert turn._bounded_history(history, 1) == history[-4:]  # nothing fits: the last user turn, whole


# ---- 5. no review fork after a spoken turn -------------------------------------------------------
def test_only_a_spoken_turn_skips_the_post_turn_review(monkeypatch):
    built = []
    _fake_hermes(monkeypatch, built)
    turn._construct_agent("agent-1", {}, None, SESSION, **turn.CALL_RUN_LIMITS)
    turn._construct_agent("agent-1", {}, None, SESSION)
    assert built[0]["skip_background_review"] is True and "skip_background_review" not in built[1]


# ---- 3. the deadline on a model call -------------------------------------------------------------
class _Clock:
    now = 0.0

    def __call__(self):
        return self.now


def _deadline(redirect_accepts=True):
    clock, calls = _Clock(), []
    agent = SimpleNamespace(redirect=lambda text: calls.append(("redirect", text)) or redirect_accepts,
                            interrupt=lambda **kw: calls.append(("interrupt", kw)))
    frames = _frames()
    watch = turn._CallDeadline(frames, clock=clock)
    watch.agent = agent
    return watch, clock, calls, frames


def test_a_silent_model_call_is_sent_again_once_then_the_run_is_ended():
    watch, clock, calls, _frames_ = _deadline()
    clock.now = turn.CALL_SILENCE_REISSUE_S - 1
    watch.check()
    assert calls == []
    clock.now = turn.CALL_SILENCE_REISSUE_S
    watch.check()
    watch.check()
    assert calls == [("redirect", turn.CALL_REISSUE_NOTE)] and not watch.timed_out  # once
    clock.now = turn.CALL_SILENCE_FAIL_S
    watch.check()
    watch.check()
    assert calls[1:] == [("interrupt", {"hard_cancel": True, "tool_reason": "model call deadline"})]
    assert watch.timed_out


def test_a_refused_reissue_is_tried_again():
    """Hermes answers False when no model request is in flight at that instant."""
    watch, clock, calls, _frames_ = _deadline(redirect_accepts=False)
    clock.now = turn.CALL_SILENCE_REISSUE_S
    watch.check()
    watch.check()
    assert len(calls) == 2 and not watch.reissued


def test_output_resets_the_deadline_and_a_running_tool_is_never_timed():
    watch, clock, calls, frames = _deadline()
    clock.now = turn.CALL_SILENCE_REISSUE_S - 1
    frames.delta("Payouts ")  # the run's own stream callback, as AIAgent calls it
    clock.now += turn.CALL_SILENCE_REISSUE_S - 1
    watch.check()
    assert calls == []
    frames.tool_start("tc-1", "posthog_query", {})
    clock.now += turn.CALL_SILENCE_FAIL_S * 4
    watch.check()
    assert calls == []
    frames.tool_complete("tc-1", "posthog_query", {}, "rows")
    clock.now += turn.CALL_SILENCE_REISSUE_S
    watch.check()
    assert [c[0] for c in calls] == ["redirect"]


def test_a_run_ended_by_the_deadline_fails_as_a_stalled_agent(env, home, monkeypatch):  # noqa: F811 -- the imported fixture
    """The voice worker already speaks a failed run; the kind is the backend's stalled-agent one."""
    stopped = []

    class Stalled(turn._CallDeadline):
        def __enter__(self):
            self.agent.interrupt = lambda **kw: stopped.append(kw)
            self.last -= turn.CALL_SILENCE_FAIL_S  # a model call with no output
            self.check()
            return self

    monkeypatch.setattr(turn, "_CallDeadline", Stalled)
    env["script"].append({"interrupted": True, "final_response": "partial"})
    turn.register_call_run(IDENTITY)
    outcome, error, output, _usage = turn._run_call(IDENTITY, REQUEST, _frames())
    assert (outcome, error, output) == ("error", turn.INTERRUPTED, "") and stopped


# ---- 2. compaction off the call path -------------------------------------------------------------
class _DB:
    def __init__(self, pressure):
        self.meta = {SESSION: {turn.PRESSURE: pressure}}

    def resolve_resume_session_id(self, sid):
        return sid

    def get_session(self, sid):
        return {"id": sid, "model_config": json.dumps(self.meta.get(sid, {})), "system_prompt": "STORED PROMPT"}

    def patch_session_model_config(self, sid, patch):
        self.meta[sid] = {**self.meta.get(sid, {}), **patch}


@pytest.fixture
def compaction(monkeypatch):
    """compact_session with Hermes's /compress core replaced: records every compaction it runs."""
    seen = SimpleNamespace(runs=[], saved=[], db=_DB(0.9), interrupts=[], gate=None, started=threading.Event(),
                           settled=[])

    def compress_now(agent, history, request, *, system_message=None, skip_without_window=False):
        seen.runs.append((agent, system_message, skip_without_window))
        seen.started.set()
        if seen.gate is not None:
            seen.gate.wait(5)
        return SimpleNamespace(status="compressed", before_tokens=55_000, after_tokens=40_000)

    manual = types.ModuleType("agent.conversation_compression_manual")
    manual.CompressRequest, manual.compress_now = lambda: "request", compress_now
    monkeypatch.setitem(sys.modules, "run_agent", types.ModuleType("run_agent"))
    monkeypatch.setitem(sys.modules, "agent", sys.modules.get("agent") or types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.conversation_compression_manual", manual)
    monkeypatch.setitem(sys.modules, "agent.conversation_compression", SimpleNamespace(
        finalize_context_engine_compression_notification=lambda _agent, committed: seen.settled.append(committed)))
    monkeypatch.setattr(turn, "_session_db", lambda _a: seen.db)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn, "save_profile", lambda a: seen.saved.append(a))
    monkeypatch.setattr(turn, "_construct_compaction_agent", lambda *_a: SimpleNamespace(
        interrupt=lambda **kw: seen.interrupts.append(kw)))
    return seen


def test_a_session_near_the_threshold_is_compacted_once_and_not_twice(compaction, home):
    """The compaction is committed to the session and its reading is cleared, so neither a second
    trigger nor the next turn compacts the same reading again."""
    assert turn.compact_session("agent-1", SESSION) == "compacted"
    assert turn.compact_session("agent-1", SESSION) == "below"
    (_agent, system_message, skip_without_window), = compaction.runs
    assert system_message == "" and skip_without_window is True  # the stored prompt is seeded, not rebuilt
    assert compaction.saved == ["agent-1"] and compaction.db.meta[SESSION][turn.PRESSURE] == 0.0
    assert compaction.settled == [True]


def test_a_session_below_the_ratio_is_left_alone(compaction, home):
    compaction.db = _DB(turn.OFF_PATH_COMPACT_RATIO - 0.01)
    assert turn.compact_session("agent-1", SESSION) == "below"
    assert compaction.runs == [] and compaction.saved == []


def test_a_conversation_with_a_turn_in_it_is_not_compacted_under_it(compaction, home):
    with turn._turn_lock("agent-1", SESSION):
        assert turn.compact_session("agent-1", SESSION) == "busy"
    assert turn.compact_session("agent-1", "lightbulb-email:abc") == "busy"  # room sessions only
    assert compaction.runs == []


def test_a_spoken_turn_stops_a_running_compaction_instead_of_waiting_for_it(compaction, home):
    compaction.gate = threading.Event()
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(turn.compact_session("agent-1", SESSION)))
    worker.start()
    assert compaction.started.wait(5)
    with turn._voice_turn_arriving("agent-1", SESSION):  # what _run_call does before it takes the lock
        assert compaction.interrupts == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]
        compaction.gate.set()
    worker.join(5)
    assert outcome == ["yielded"] and compaction.saved == [] and compaction.settled == [False]
    assert compaction.db.meta[SESSION][turn.PRESSURE] == 0.9  # still owed: the turn's own end compacts
    assert turn._turn_lock("agent-1", SESSION).acquire(blocking=False)
    turn._turn_lock("agent-1", SESSION).release()


def _lock_is_free():
    lock = turn._turn_lock("agent-1", SESSION)
    free = lock.acquire(blocking=False)
    if free:
        lock.release()
    return free


def test_a_compaction_that_starts_while_a_spoken_turn_is_arriving_is_refused(compaction, home):
    """The gap the first version left open: the turn has looked for a compaction, found none, and
    does not hold the lock yet. Nothing may take the lock in that gap."""
    with turn._voice_turn_arriving("agent-1", SESSION):
        assert turn.compact_session("agent-1", SESSION) == "yielded"
        assert _lock_is_free()  # the turn takes it without waiting
    assert compaction.runs == [] and compaction.db.meta[SESSION][turn.PRESSURE] == 0.9
    assert turn.compact_session("agent-1", SESSION) == "compacted"  # nobody is arriving any more


def test_a_turn_that_arrives_as_the_compaction_takes_the_lock_gets_it_back_at_once(compaction, home, monkeypatch):
    """The turn arrives after compact_session's first look and before its lock: the second look,
    with the lock held, stands down."""
    lock, arrivals = turn._turn_lock("agent-1", SESSION), []

    class ArrivesDuringAcquire:
        def acquire(self, blocking=True):
            got = lock.acquire(blocking=blocking)
            arrivals.append(turn._voice_turn_arriving("agent-1", SESSION))
            arrivals[-1].__enter__()
            return got

        release = staticmethod(lock.release)

    monkeypatch.setattr(turn, "_turn_lock", lambda *_a: ArrivesDuringAcquire())
    try:
        assert turn.compact_session("agent-1", SESSION) == "yielded"
        assert lock.acquire(blocking=False)
        lock.release()
    finally:
        arrivals[0].__exit__(None, None, None)
    assert compaction.runs == [] and compaction.interrupts == [] and turn._compacting == {}


def test_a_turn_that_arrives_before_the_compaction_has_an_agent_stops_it_between_steps(compaction, home, monkeypatch):
    """The compaction holds the lock but has nothing to interrupt yet: it stops at its next step."""
    stack = turn.contextlib.ExitStack()

    def arrive(_agent_id):
        stack.enter_context(turn._voice_turn_arriving("agent-1", SESSION))

    monkeypatch.setattr(turn.sync, "ensure_profile", arrive)
    with stack:
        assert turn.compact_session("agent-1", SESSION) == "yielded"
        assert _lock_is_free()
    assert compaction.runs == [] and compaction.saved == [] and compaction.db.meta[SESSION][turn.PRESSURE] == 0.9


def test_two_spoken_turns_waiting_are_counted(compaction, home):
    first, second = turn._voice_turn_arriving("agent-1", SESSION), turn._voice_turn_arriving("agent-1", SESSION)
    first.__enter__()
    second.__enter__()
    first.__exit__(None, None, None)  # one has its lock; the other still waits
    assert turn.compact_session("agent-1", SESSION) == "yielded" and compaction.runs == []
    second.__exit__(None, None, None)
    assert turn._voice_arriving == {}
    assert turn.compact_session("agent-1", SESSION) == "compacted"


def test_a_spoken_turn_holding_its_lock_is_no_longer_arriving(home):
    with turn._voice_turn_lock("agent-1", SESSION, None):
        assert turn._voice_arriving == {} and not _lock_is_free()
    assert _lock_is_free()


@pytest.mark.parametrize("failure", [RuntimeError("lock machinery broke"), KeyboardInterrupt()], ids=["error", "cancelled"])
def test_the_arrival_is_cleared_when_taking_the_lock_fails(home, monkeypatch, failure):
    def broken(*_a, **_k):
        raise failure

    monkeypatch.setattr(turn, "_acquire_unless_aborted", broken)
    with pytest.raises(type(failure)), turn._voice_turn_lock("agent-1", SESSION, "run-1"):
        raise AssertionError("no lock, no body")
    assert turn._voice_arriving == {}


def test_the_arrival_is_cleared_when_the_turn_fails_or_gives_up_waiting(home):
    with pytest.raises(RuntimeError), turn._voice_turn_lock("agent-1", SESSION, None):
        raise RuntimeError("the turn failed")
    assert turn._voice_arriving == {} and _lock_is_free()
    turn.RUNS["run-stopped"] = {"aborted": True}
    try:
        # Another turn holds the conversation; this one was stopped while queued and gives up.
        with turn._turn_lock("agent-1", SESSION), turn._voice_turn_lock("agent-1", SESSION, "run-stopped"):
            assert turn._voice_arriving == {}
    finally:
        del turn.RUNS["run-stopped"]
    assert turn._voice_arriving == {} and _lock_is_free()


def test_the_compaction_agent_is_hermes_hygiene_shaped(monkeypatch):
    built = []
    _fake_hermes(monkeypatch, built)

    class AIAgent:
        def __init__(self, **kw):
            built.append(kw)
            self.session_id = kw["session_id"]

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=AIAgent))
    monkeypatch.setattr(turn, "_session_tip", lambda _db, key: key)
    agent = turn._construct_compaction_agent("agent-1", SESSION, _DB(0.9))
    kw = built[0]
    assert kw["enabled_toolsets"] == ["memory"] and kw["skip_memory"] is True and kw["max_iterations"] == 4
    assert kw["skip_background_review"] is True and kw["session_id"] == SESSION
    assert agent._cached_system_prompt == "STORED PROMPT" and agent.platform == "gateway_hygiene"


def test_a_spoken_turn_compacts_after_its_answer_is_delivered_and_saved(monkeypatch, posted):
    order = []
    _closing(monkeypatch)
    monkeypatch.setattr(turn, "save_profile", lambda a: order.append("save"))
    monkeypatch.setattr(turn, "_run_call", lambda *_a: ("completed", None, "ok", dict(turn.ZERO_USAGE)))
    monkeypatch.setattr(turn.VoiceFramePublisher, "finished", lambda self, *a: order.append("finished"))
    monkeypatch.setattr(turn, "compact_session", lambda a, key: order.append(("compact", a, key)))
    turn.register_call_run(IDENTITY)
    turn.run_call_turn(IDENTITY, REQUEST)
    assert order == ["finished", "save", ("compact", "agent-1", SESSION)]


def test_going_live_compacts_the_rooms_session_off_the_request_thread(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    server._roster_cache = (0.0, ())
    done, seen = threading.Event(), []
    monkeypatch.setattr(sync, "restore_profile", lambda _a: 0.0)
    monkeypatch.setattr(sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn, "compact_session", lambda a, key: seen.append((a, key, turn._busy)) or done.set())
    assert server._profile_warm({"agentId": "ceo", "roomId": ROOM})["payload"] == {"warming": True}
    assert done.wait(2)
    assert seen == [("ceo", turn.lightbulb_session_key("ceo", ROOM), 1)]  # counted as a turn while it runs
    server._roster_cache = (0.0, ())


def test_a_warm_without_a_room_compacts_nothing(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    server._roster_cache = (0.0, ())
    done = threading.Event()
    monkeypatch.setattr(sync, "restore_profile", lambda _a: 0.0)
    monkeypatch.setattr(sync, "ensure_profile", lambda _a: done.set())
    monkeypatch.setattr(turn, "compact_session", lambda *_a: (_ for _ in ()).throw(AssertionError))
    server._profile_warm({"agentId": "ceo"})
    assert done.wait(2)
    server._roster_cache = (0.0, ())


def test_context_pressure_is_hermes_last_real_prompt_over_its_threshold():
    compressor = SimpleNamespace(last_prompt_tokens=54_000, threshold_tokens=60_000)
    assert turn._context_pressure(SimpleNamespace(context_compressor=compressor)) == 0.9
    compressor.last_prompt_tokens = -1  # Hermes's "awaiting real usage" sentinel
    assert turn._context_pressure(SimpleNamespace(context_compressor=compressor)) == 0.0
    assert turn._context_pressure(SimpleNamespace()) == 0.0
