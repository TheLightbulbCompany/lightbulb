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
    assert code == 202 and accepted == {"run_id": RUN, "status": "queued", "session_id": KEY}
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
    ({**IDENTITY, "mode": "router"}, REQUEST),
    ({**IDENTITY, "session": {"kind": "task"}}, REQUEST),
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
    assert _post(headers={**AUTH, "X-Lightbulb-Local-Session-Token": "grant-jwt"})[0] == 202
    threads[0].run_now()

    assert not any(name.startswith("PAPERCLIP_") for name in seen["overlay"])
    ctx = seen["ctx"]
    assert ctx["family"] == "task" and ctx["roomId"] is None and ctx["sessionKey"] == KEY
    assert ctx["runId"] == RUN and ctx["localSessionToken"] == "grant-jwt"
    assert seen["limits"] == {"max_iterations": 40}
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


def test_paperclip_routes_are_not_v1():
    assert runs.handle("/internal/paperclip/v1/runs", "POST", AUTH, BODY) is None


from tests.test_native_sessions import hermes  # noqa: E402,F401  (fixture)
