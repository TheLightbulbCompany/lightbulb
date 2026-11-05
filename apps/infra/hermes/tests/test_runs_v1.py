"""The generic run contract /internal/v1/runs (spec 02 §5.1): a Worker turn keyed by task ids.

It runs on the Paperclip run machinery (idempotent create, durable S3 record, heartbeat, stop),
so these mirror test_paperclip_gateway.py / test_paperclip_run_record.py with a task identity.
"""
import json
import pathlib
from contextlib import contextmanager

import pytest

from bridge import runs, server, sync, turn
from tests.test_paperclip_gateway import _capturing_threading
from tests.test_paperclip_run_record import FakeS3

AUTH = {"authorization": "Bearer gw-test"}
RUN = "0b5c6c39-6a43-5b7e-9d1a-3f0f2d7c1a11"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
KEY = f"agent:{AGENT}:lightbulb:task:{TASK}"
IDENTITY = {"runId": RUN, "ownerId": "org_test", "taskId": TASK, "attempt": 1, "agentId": AGENT,
            "sessionKey": KEY, "mode": "worker", "session": {"kind": "workspace"}}
REQUEST = {"input": "Reconcile the October invoices.", "instructions": "You are the Finance worker.",
           "effects": "off", "limits": {"max_iterations": 40}}
BODY = {"identity": IDENTITY, "request": REQUEST}
CREATE = "/internal/v1/runs"
POLL = f"{CREATE}/{RUN}"


@pytest.fixture
def threads(monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(runs, "threading", threading)
    return created


@pytest.fixture
def quiet_turn(home, monkeypatch):
    """Everything around the turn body that would touch S3, the backend or the profile."""
    (home / "profiles" / AGENT).mkdir(parents=True)
    for name in ("restore_profile", "save_profile", "report_usage"):
        monkeypatch.setattr(turn, name, lambda *a, **k: None)
    for name in ("restore_root_auth", "save_root_auth", "ensure_profile", "normalize_profile_backends"):
        monkeypatch.setattr(turn.sync, name, lambda *a, **k: None)


class Agent:
    model, session_input_tokens, session_output_tokens = "model-w", 11, 5
    session_cache_read_tokens = session_cache_write_tokens = 0

    def __init__(self, reply="reconciled"):
        self.reply, self.interrupts, self.calls = reply, [], []

    def run_conversation(self, **kw):
        self.calls.append(kw)
        return {"api_calls": 1, "final_response": self.reply}

    def interrupt(self, **kw):
        self.interrupts.append(kw)


def _post(body=BODY, headers=AUTH):
    return server.handle_plugin(CREATE, "POST", headers, body)


def _poll(identity=IDENTITY):
    return server.handle_plugin(f"{CREATE}/{identity['runId']}", "GET", AUTH, {"identity": identity})


def _stop(identity=IDENTITY):
    return server.handle_plugin(f"{CREATE}/{identity['runId']}/stop", "POST", AUTH, {"identity": identity})


def test_create_poll_complete(quiet_turn, threads, monkeypatch):
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)

    code, accepted = _post()
    assert code == 202 and accepted == {"run_id": RUN, "status": "queued", "session_id": KEY, "recentTools": []}
    threads[0].run_now()

    code, status = _poll()
    assert code == 200
    assert status["status"] == "completed" and status["output"] == "reconciled"
    assert status["usage"] == {"model": "model-w", "input_tokens": 11, "output_tokens": 5, "cached_input_tokens": 0}
    assert agent.calls[0]["user_message"] == REQUEST["input"]
    assert agent.calls[0]["system_message"].startswith(REQUEST["instructions"])


def test_replay_and_conflict(threads):
    assert _post()[0] == 202
    code, replay = _post()
    assert (code, replay["run_id"]) == (200, RUN)
    code, conflict = _post({"identity": IDENTITY, "request": {**REQUEST, "input": "something else"}})
    assert (code, conflict) == (409, {"error": "idempotency_conflict"})
    assert len(threads) == 1


def test_stop_queued_and_running(quiet_turn, threads, monkeypatch):
    called = []
    monkeypatch.setattr(turn, "_run_task_turn", lambda *a, **k: called.append(a))
    assert _post()[0] == 202
    code, stopping = _stop()
    assert (code, stopping) == (202, {"run_id": RUN, "status": "stopping", "session_id": KEY})
    threads[0].run_now()
    assert called == [] and _poll()[1]["status"] == "cancelled"
    assert _stop()[0] == 200  # terminal

    turn.RUNS.clear()
    agent = Agent()
    assert _post()[0] == 202
    turn.attach_agent(RUN, agent)
    assert _stop()[0] == 202
    assert agent.interrupts == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    return fake


def test_poll_from_s3_record_on_other_microvm(home, s3):
    turn.register_durable_run(IDENTITY, REQUEST, kind="task")
    turn.finish_paperclip_run(RUN, "completed", output="done elsewhere", error=None, usage=turn.ZERO_USAGE)
    assert f"{sync.S3_PREFIX}/{AGENT}/.runs/{RUN}.json" in s3.objects
    assert not any("/.paperclip-runs/" in k for k in s3.objects)
    turn.RUNS.clear()  # the poll lands on the split session's other microVM

    code, status = _poll()
    assert (code, status["status"], status["output"]) == (200, "completed", "done elsewhere")
    assert _poll({**IDENTITY, "attempt": 2})[0] == 404  # a record answers only its own identity


def test_s3_error_is_503_not_404(home, s3, monkeypatch):
    def throttled(**_kw):
        raise RuntimeError("SlowDown")

    monkeypatch.setattr(s3, "get_object", throttled)
    assert _poll() == (503, {"error": "run_status_unavailable"})


def test_503_while_recycling(threads, monkeypatch):
    monkeypatch.setattr(turn, "reserve_turn", lambda: False)
    assert _post() == (503, {"error": "recycling"})
    assert threads == []


@pytest.mark.parametrize("identity, request_", [
    ({**IDENTITY, "mode": "planner"}, REQUEST),
    ({**IDENTITY, "session": {"kind": "shared"}}, REQUEST),  # an unknown session kind (task is V3's, accepted)
    (IDENTITY, {**REQUEST, "effects": "required"}),
])
def test_unsupported_mode_and_session_kind(threads, identity, request_):
    assert _post({"identity": identity, "request": request_}) == (400, {"error": "unsupported_in_this_version"})
    assert threads == []


def test_owner_mismatch_is_403(threads):
    assert _post({"identity": {**IDENTITY, "ownerId": "org_other"}, "request": REQUEST})[0] == 403
    assert _poll({**IDENTITY, "ownerId": "org_other"})[0] == 403


def test_requires_gateway_token(threads):
    assert _post(headers={})[0] == 401
    assert _post(headers={"authorization": "Bearer wrong"})[0] == 401
    assert server.handle_plugin(POLL, "GET", {}, {"identity": IDENTITY})[0] == 401


def test_run_budget_over_cap_is_400(threads):
    over = {**REQUEST, "limits": {"run_budget_seconds": runs.MAX_RUN_BUDGET_S + 1}}
    assert _post({"identity": IDENTITY, "request": over})[0] == 400
    at_cap = {**REQUEST, "limits": {"run_budget_seconds": 23400}}
    assert _post({"identity": IDENTITY, "request": at_cap})[0] == 202


def _schema_path() -> pathlib.Path | None:
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "packages/shared/fixtures/super-agent/tasks-schema.json"
        if candidate.exists():
            return candidate
    return None


def test_session_key_regex():
    assert runs.SESSION_KEY_RE.fullmatch(KEY)
    for bad in (f"agent:{AGENT}:lightbulb:channel:{TASK}", f"agent:ceo:lightbulb:task:{TASK}", KEY + ":x"):
        assert not runs.SESSION_KEY_RE.fullmatch(bad)
    # The key must name this identity's own agent and task.
    other = {**IDENTITY, "sessionKey": f"agent:{AGENT}:lightbulb:task:{RUN}"}
    assert _post({"identity": other, "request": REQUEST})[0] == 400
    path = _schema_path()
    pattern = path and (json.loads(path.read_text()).get("sessionKeys") or {}).get("worker")
    if pattern:  # the frozen schema names it once core.tasks_schema exports it
        assert pattern == runs.SESSION_KEY_RE.pattern


def test_worker_session_key_and_resume_same_transcript(hermes, home):
    from tests import test_native_sessions as ns
    _acquired, built, dbs = hermes

    def first_attempt(agent):
        agent._session_db.append_message(agent.session_id, "user", "attempt 1 asked")
        agent._session_db.append_message(agent.session_id, "assistant", "attempt 1 did half")
        return {"api_calls": 1, "final_response": "partial"}

    ns.SCRIPT.append(first_attempt)
    attempt1 = {**IDENTITY}
    attempt2 = {**IDENTITY, "attempt": 2, "runId": "1c2d3e4f-5a6b-5c7d-8e9f-0a1b2c3d4e5f"}
    for identity in (attempt1, attempt2):
        turn.register_durable_run(identity, REQUEST, kind="task")
        assert turn._run_task_turn(identity, REQUEST, family="task")[0] == "completed"
    first, second = built
    assert first.kw["session_id"] == second.kw["session_id"] == KEY
    # The retry runs on the first attempt's transcript.
    history = second.calls[0][0]["conversation_history"]
    assert [m["content"] for m in history] == ["attempt 1 asked", "attempt 1 did half"]


def test_task_family_ctx_has_no_paperclip_env(quiet_turn, threads, monkeypatch):
    seen = {}

    @contextmanager
    def secret_scope(agent_id, overlay=None):
        seen["overlay"] = dict(overlay or {})
        yield

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)

    def construct(agent_id, body, frames, session_key, **limits):
        seen["limits"] = limits
        path = turn._turn_context_path(turn._profile_dir(AGENT), KEY)
        seen["ctx"] = json.loads(path.read_text())
        return Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)
    with_secret = {"identity": {**IDENTITY, "runSecret": "s3"}, "request": REQUEST}
    assert _post(with_secret, headers={**AUTH, "X-Lightbulb-Local-Session-Token": "grant-jwt"})[0] == 202
    threads[0].run_now()

    assert not any(name.startswith("PAPERCLIP_") for name in seen["overlay"])
    assert seen["overlay"]["LIGHTBULB_RUN_ID"] == RUN  # lightbulb-api.sh sends it as X-Lightbulb-Run (spec 05 P1)
    assert seen["overlay"]["LIGHTBULB_RUN_SECRET"] == "s3"  # …with X-Lightbulb-Run-Secret
    ctx = seen["ctx"]
    assert ctx["family"] == "task" and ctx["roomId"] is None and ctx["sessionKey"] == KEY
    assert ctx["runId"] == RUN and ctx["localSessionToken"] == "grant-jwt"
    assert ctx["taskId"] == TASK and ctx["attempt"] == 1 and ctx["runSecret"] == "s3"  # Lane Q: the plugin's ask speaks for this attempt
    assert seen["limits"]["max_iterations"] == 40 and callable(seen["limits"]["tool_complete_callback"])
    assert _poll()[1]["status"] == "completed"


def test_run_budget_interrupts_and_fails_the_run(quiet_turn, threads, monkeypatch):
    class Slow(Agent):
        def run_conversation(self, **kw):
            assert self.budget_hit.wait(2)
            return {"api_calls": 1, "interrupted": True, "final_response": ""}

        def interrupt(self, **kw):
            super().interrupt(**kw)
            self.budget_hit.set()

    import threading
    agent = Slow()
    agent.budget_hit = threading.Event()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    request = {**REQUEST, "limits": {"run_budget_seconds": 60}}
    real_timer = threading.Timer
    monkeypatch.setattr(turn.threading, "Timer", lambda _s, fn, args=(): real_timer(0.01, fn, args))
    assert _post({"identity": IDENTITY, "request": request})[0] == 202
    threads[0].run_now()
    status = _poll()[1]
    assert (status["status"], status["error"]) == ("failed", "run_budget_exceeded")


def test_run_budget_raise_after_interrupt_is_budget_exceeded(quiet_turn, threads, monkeypatch):
    import threading

    class Raises(Agent):
        def run_conversation(self, **kw):
            assert self.budget_hit.wait(2)
            raise RuntimeError("interrupted mid-request")

        def interrupt(self, **kw):
            self.budget_hit.set()

    agent = Raises()
    agent.budget_hit = threading.Event()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    real_timer = threading.Timer
    monkeypatch.setattr(turn.threading, "Timer", lambda _s, fn, args=(): real_timer(0.01, fn, args))
    assert _post({"identity": IDENTITY, "request": {**REQUEST, "limits": {"run_budget_seconds": 60}}})[0] == 202
    threads[0].run_now()
    status = _poll()[1]
    assert (status["status"], status["error"]) == ("failed", "run_budget_exceeded")


def test_run_budget_spent_before_the_conversation_never_starts_it(quiet_turn, threads, monkeypatch):
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)

    class Expired:  # the budget runs out between starting the timer and calling the agent
        def __init__(self, _s, fn, args=()):
            self.fn, self.args, self.daemon = fn, args, False

        def start(self):
            self.fn(*self.args)

        def cancel(self):
            pass

    monkeypatch.setattr(turn.threading, "Timer", Expired)
    assert _post({"identity": IDENTITY, "request": {**REQUEST, "limits": {"run_budget_seconds": 60}}})[0] == 202
    threads[0].run_now()
    status = _poll()[1]
    assert (status["status"], status["error"]) == ("failed", "run_budget_exceeded")
    assert agent.calls == []


def test_paperclip_routes_are_not_v1():
    assert runs.handle("/internal/paperclip/v1/runs", "POST", AUTH, BODY) is None


# ---- steer (user|input) and recentTools (Task 2; spec 11 C5 and tier-2 steer) ----------------------
import sys  # noqa: E402
import types  # noqa: E402


class Steerable(Agent):
    def __init__(self):
        super().__init__()
        self.steers = []

    def steer(self, text):
        self.steers.append(text)
        return True


@pytest.fixture
def lb_inject(monkeypatch):
    """F3 with the fork's real gate (tests/fake_lb_inject.py)."""
    from tests import fake_lb_inject

    monkeypatch.setitem(sys.modules, "agent", sys.modules.get("agent") or types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.lb_inject", fake_lb_inject)
    return fake_lb_inject


class F3Agent(Agent):
    """run_conversation as Hermes + F3 drive it: deliver() before each model call, a tool round
    (tool_complete_callback) when `tools` is set, close() and pending_inputs at the end."""

    def __init__(self, f3, tools=0, between=None):
        super().__init__()
        self.f3, self.tools, self.between, self.messages_seen, self.steers = f3, tools, between, [], []
        self.tool_complete = None

    def steer(self, text):
        self.steers.append(text)
        return True

    def run_conversation(self, **kw):
        self.calls.append(kw)
        messages = [{"role": "user", "content": kw["user_message"]}]
        self.f3.deliver(self, messages)  # iteration prep: the turn is open
        if self.between:
            self.between()  # something arrives mid-turn
            self.between = None
        for i in range(self.tools if len(self.calls) == 1 else 0):
            messages.append({"role": "tool", "content": "ok"})
            self.tool_complete(f"c{i}", "terminal", {"i": i}, "ok")
            self.f3.deliver(self, messages)  # the next model call's iteration prep
        self.messages_seen.append(messages)
        self.f3.close(self)
        return {"api_calls": 1, "final_response": f"pass {len(self.calls)}", "pending_inputs": self.f3.take_pending(self)}


def _f3_run(monkeypatch, agent):
    def construct(*_a, **limits):
        agent.tool_complete = limits["tool_complete_callback"]
        return agent

    monkeypatch.setattr(turn, "_construct_agent", construct)


def _steer(text, kind, identity=IDENTITY):
    path = f"{CREATE}/{identity['runId']}/steer"
    return server.handle_plugin(path, "POST", AUTH, {"identity": identity, "text": text, "kind": kind})


def _running(threads, agent):
    assert _post()[0] == 202
    turn.attach_agent(RUN, agent)


def test_user_steer_reaches_agent_steer(threads):
    agent = Steerable()
    _running(threads, agent)
    assert _steer("check Q3 too", "user") == (200, {"accepted": True})
    assert agent.steers == ["check Q3 too"]


def test_input_steer_uses_inject_not_steer(threads, lb_inject):
    agent = Steerable()
    agent._lb_inputs_open = True  # mid-turn: F3's deliver() has opened it
    _running(threads, agent)
    assert _steer("you have been idle 20 minutes", "input") == (200, {"accepted": True})
    assert agent._lb_inputs == [("input", "you have been idle 20 minutes")]
    assert agent.steers == []


def test_input_steer_without_f3_is_refused_not_steered(threads, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.lb_inject", None)  # import fails: an image without the series
    agent = Steerable()
    _running(threads, agent)
    assert _steer("nudge", "input") == (200, {"accepted": False})
    assert agent.steers == []


def test_input_steer_on_queued_run_is_delivered_after_the_first_tool_round(quiet_turn, threads, lb_inject, monkeypatch):
    agent = F3Agent(lb_inject, tools=1)
    _f3_run(monkeypatch, agent)
    assert _post()[0] == 202
    assert _steer("nudge", "input") == (200, {"accepted": True})  # held: no open turn yet
    threads[0].run_now()
    rows = [m for m in agent.messages_seen[0] if m.get("display_kind") == lb_inject.DISPLAY_KIND]
    assert len(rows) == 1 and "nudge" in rows[0]["content"]
    assert len(agent.calls) == 1 and agent.steers == []


def test_input_held_with_no_tool_round_is_answered_in_the_same_run(quiet_turn, threads, lb_inject, monkeypatch):
    agent = F3Agent(lb_inject, tools=0)
    _f3_run(monkeypatch, agent)
    assert _post()[0] == 202
    assert _steer("nudge", "input") == (200, {"accepted": True})
    threads[0].run_now()
    assert len(agent.calls) == 2  # never dropped: a second pass carries it, framed as data
    assert agent.calls[1]["user_message"] == lb_inject.input_row("input", "nudge")["content"]
    assert _poll()[1]["output"] == "pass 1\n\npass 2"


def test_late_pending_inputs_are_answered_in_the_same_run(quiet_turn, threads, lb_inject, monkeypatch):
    agent = F3Agent(lb_inject, tools=0, between=lambda: _steer("late nudge", "input"))
    _f3_run(monkeypatch, agent)
    assert _post()[0] == 202
    threads[0].run_now()
    # Injected into the open turn, never delivered (no tool round after it), handed back as pending_inputs.
    assert agent.calls[1]["user_message"] == lb_inject.input_row("input", "late nudge")["content"]


def test_input_steer_after_the_run_is_refused(quiet_turn, threads, lb_inject, monkeypatch):
    agent = F3Agent(lb_inject)
    _f3_run(monkeypatch, agent)
    assert _post()[0] == 202
    threads[0].run_now()
    assert _steer("too late", "input") == (200, {"accepted": False})


def test_user_steer_on_queued_run_rides_the_input(quiet_turn, threads, monkeypatch):
    agent = Steerable()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    assert _post()[0] == 202
    assert _steer("also March", "user") == (200, {"accepted": True})
    threads[0].run_now()
    assert agent.calls[0]["user_message"] == f"{REQUEST['input']}\n\nalso March"


def test_a_steer_after_the_last_tool_batch_is_answered_in_the_same_run(quiet_turn, threads, monkeypatch):
    class Late(Agent):
        def run_conversation(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return {"api_calls": 1, "final_response": "first", "pending_steer": "also March", "messages": [1]}
            return {"api_calls": 1, "final_response": "march too"}

    agent = Late()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    assert _post()[0] == 202
    threads[0].run_now()
    assert agent.calls[1]["user_message"] == "also March" and agent.calls[1]["conversation_history"] == [1]
    assert _poll()[1]["output"] == "first\n\nmarch too"


def test_steer_on_finished_run_is_false(threads):
    assert _post()[0] == 202
    turn.finish_paperclip_run(RUN, "completed", output="x", error=None, usage=turn.ZERO_USAGE)
    assert _steer("late", "user") == (200, {"accepted": False})


def test_steer_validation(threads):
    assert _post()[0] == 202
    assert _steer("x", "admin")[0] == 400
    assert _steer("", "user")[0] == 400
    assert _steer("x" * (turn.MAX_FOLLOWUP_TEXT_BYTES + 1), "user")[0] == 400
    assert _steer("x", "user", {**IDENTITY, "attempt": 2})[0] == 404


def _complete_tools(callback, n=25):
    for i in range(n):
        callback(f"call-{i}", f"tool_{i}", {"b": i, "a": "secret-value"}, "ok")


def test_status_has_recent_tools(threads):
    assert _post()[0] == 202
    record = turn.task_tool_recorder(RUN)
    _complete_tools(record)
    record("call-x", "terminal", {"a": 1, "b": 2}, {"error": "denied"})
    record("call-y", "terminal", {"b": 2, "a": 1}, '{"error": "boom", "error_type": "TimeoutError"}')
    tools = _poll()[1]["recentTools"]
    assert len(tools) == 20 and tools[-1]["name"] == "terminal" and tools[0]["name"] == "tool_7"
    assert tools[-2]["argsHash"] == tools[-1]["argsHash"] and len(tools[-1]["argsHash"]) == 16
    assert tools[-2] == {"name": "terminal", "argsHash": tools[-2]["argsHash"], "ok": False, "errorClass": "ToolError"}
    assert tools[-1]["errorClass"] == "TimeoutError"
    assert tools[0]["ok"] is True and tools[0]["errorClass"] is None


def test_recent_tools_survive_poll_from_s3(home, s3):
    turn.register_durable_run(IDENTITY, REQUEST, kind="task")
    _complete_tools(turn.task_tool_recorder(RUN), 3)
    turn._put_running_record(RUN)  # a heartbeat
    live = _poll()[1]["recentTools"]
    turn.RUNS.clear()
    assert _poll()[1]["recentTools"] == live and len(live) == 3


def test_recent_tools_hold_no_args_or_results(threads):
    assert _post()[0] == 202
    turn.task_tool_recorder(RUN)("c", "browser", {"url": "https://secret.example/token=abc"}, "RESULT-TEXT")
    serialized = json.dumps(_poll()[1])
    assert "secret" not in serialized and "RESULT-TEXT" not in serialized


from tests.test_native_sessions import hermes  # noqa: E402,F401  (fixture)
