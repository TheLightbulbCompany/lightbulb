"""Native Paperclip Hermes run lifecycle inside the AgentCore bridge."""

import bridge
import json
from contextlib import contextmanager

import pytest

from bridge import server, turn

AUTH = {"authorization": "Bearer gw-test"}
RUN = "run-123"
SESSION = "paperclip:company:company-1:agent:agent-1:issue:issue-1"
IDENTITY = {
    "runId": RUN,
    "companyId": "company-1",
    "agentId": "agent-1",
    "sessionId": SESSION,
    "issueId": "issue-1",
    "paperclipApiUrl": "http://paperclip.internal:3100",
}
REQUEST = {
    "input": "# Task\nKeep **this** exact markdown.\n",
    "instructions": "Follow these exact native instructions; do not rewrite them.\n",
    "session_id": SESSION,
    "wake_payload": {"reason": "comment", "nested": [1, 2]},
}
CREATE_PATH = "/internal/paperclip/v1/runs"
RUN_PATH = f"{CREATE_PATH}/{RUN}"
BODY = {"identity": IDENTITY, "request": REQUEST}


def _capturing_threading():
    instances = []

    class Thread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")
            self.args = kwargs.get("args", ())
            instances.append(self)

        def start(self):
            pass

        def run_now(self):
            self.target(*self.args)

    class Module:
        pass

    Module.Thread = Thread
    return Module, instances


def test_create_is_only_an_acceptance_receipt_until_the_real_turn_finishes(monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)

    code, accepted = server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)
    status_code, status = server.handle_plugin(RUN_PATH, "GET", AUTH, {"identity": IDENTITY})

    assert code == 202
    assert accepted == {"run_id": RUN, "status": "queued", "session_id": SESSION}
    assert status_code == 200
    assert status == accepted
    assert len(created) == 1


def test_same_idempotent_request_replays_but_changed_request_conflicts(monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)

    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    replay_code, replay = server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)
    changed = {"identity": IDENTITY, "request": {**REQUEST, "input": "different"}}
    conflict_code, conflict = server.handle_plugin(CREATE_PATH, "POST", AUTH, changed)

    assert replay_code == 200
    assert replay == {"run_id": RUN, "status": "queued", "session_id": SESSION}
    assert conflict_code == 409
    assert conflict["error"] == "idempotency_conflict"
    assert len(created) == 1


def test_create_rejects_unauthorized_or_incoherent_identity(monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)

    assert server.handle_plugin(CREATE_PATH, "POST", {}, BODY)[0] == 401
    mismatch = {"identity": IDENTITY, "request": {**REQUEST, "session_id": SESSION + "-other"}}
    code, result = server.handle_plugin(CREATE_PATH, "POST", AUTH, mismatch)
    assert code == 400
    assert result["error"] == "invalid_paperclip_run"
    assert created == []


def test_native_context_completes_with_real_output_usage_and_session(home, monkeypatch):
    profile = home / "profiles" / "agent-1"
    profile.mkdir(parents=True)
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)
    monkeypatch.setattr(turn, "restore_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn, "save_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    usage_reports = []
    monkeypatch.setattr(turn, "report_usage", lambda *args, **kwargs: usage_reports.append((args, kwargs)))

    seen = {}

    @contextmanager
    def secret_scope(agent_id, overlay=None):
        seen["secret_agent"] = agent_id
        seen["secret_overlay"] = dict(overlay or {})
        yield

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)

    class Agent:
        model = "model-native"
        session_input_tokens = 12
        session_output_tokens = 7
        session_cache_read_tokens = 3
        session_cache_write_tokens = 0

        def run_conversation(self, **kwargs):
            seen["run_kwargs"] = kwargs
            return {
                "api_calls": 1, "final_response": "actual terminal output", "failed": False,
                "completed": True, "tools": [{"name": "shell"}],
            }

    def construct(agent_id, body, frames, session_key):
        import json
        import hashlib
        root = turn._profile_dir(agent_id)
        state = json.loads(turn._desktop_state_path(root, RUN).read_text())
        assert state["ctx"] == {"runId": RUN, "agentId": agent_id, "ownerId": bridge.OWNER,
                                "apiUrl": turn.API, "hookToken": bridge.HOOK_TOKEN,
                                "family": "paperclip", "sessionKey": SESSION}
        marker = root / turn.TURN_CONTEXT_DIRNAME / ("desktop-active-" + hashlib.sha256(SESSION.encode()).hexdigest() + ".json")
        assert json.loads(marker.read_text()) == {"runId": RUN}
        seen["construct"] = (agent_id, body, frames, session_key)
        return Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)

    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    created[0].run_now()
    code, status = server.handle_plugin(RUN_PATH, "GET", AUTH, {"identity": IDENTITY})

    assert code == 200
    assert status == {
        "run_id": RUN,
        "status": "completed",
        "session_id": SESSION,
        "output": "actual terminal output",
        "usage": {
            "model": "model-native",
            "input_tokens": 12,
            "output_tokens": 7,
            "cached_input_tokens": 3,
        },
    }
    assert seen["run_kwargs"] == {
        "user_message": REQUEST["input"],
        # the clone and chat rules ride every prompt; an issue run's also says where its worktree goes
        "system_message": f"{REQUEST['instructions']}\n\n{turn._repo_rule("agent-1")}\n\n"
        f"{turn.worktrees.prompt_line("agent-1", "issue-1", [])}\n{turn.CHAT_STYLE}",
    }
    assert seen["construct"] == ("agent-1", {"sessionKey": SESSION}, None, SESSION)
    assert not turn._desktop_state_path(turn._profile_dir("agent-1"), RUN).exists()
    assert seen["secret_overlay"] == {
        "PAPERCLIP_API_URL": "http://paperclip.internal:3100",
        "PAPERCLIP_AGENT_ID": "agent-1",
        "PAPERCLIP_COMPANY_ID": "company-1",
        "PAPERCLIP_RUN_ID": RUN,
        "PAPERCLIP_TASK_ID": "issue-1",
    }
    usage_args, usage_kwargs = usage_reports[0]
    assert usage_kwargs == {}
    assert usage_args[:4] == (SESSION, "agent-1", RUN, True)
    assert usage_args[5]["inputTokens"] == 12
    assert usage_args[5]["outputTokens"] == 7


def test_native_turn_waits_for_its_own_conversation(home, monkeypatch):
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    constructed = threading.Event()
    def construct(*_args):
        constructed.set()
        return SimpleNamespace(run_conversation=lambda **_kw: {"api_calls": 1, "final_response": "ok"})
    monkeypatch.setattr(turn, "_construct_agent", construct)
    turn.register_paperclip_run(IDENTITY, REQUEST)
    worker = threading.Thread(target=turn._run_native_paperclip, args=(IDENTITY, REQUEST))
    try:
        with turn._turn_lock("agent-1", IDENTITY["sessionId"]):
            worker.start()
            assert not constructed.wait(0.1), "native turns must not replace an active run's session context"
    finally:
        worker.join(2)
    assert constructed.is_set() and not worker.is_alive()


def test_failed_turn_and_unknown_run_are_honest_terminal_or_missing(home, monkeypatch):
    profile = home / "profiles" / "agent-1"
    profile.mkdir(parents=True)
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)
    monkeypatch.setattr(turn, "restore_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn, "save_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn, "report_usage", lambda *args, **kwargs: None)

    class Agent:
        model = "model-native"
        session_input_tokens = 0
        session_output_tokens = 0
        session_cache_read_tokens = 0
        session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            raise RuntimeError("do not leak this detail")

    monkeypatch.setattr(turn, "_construct_agent", lambda *args: Agent())
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    created[0].run_now()

    code, status = server.handle_plugin(RUN_PATH, "GET", AUTH, {"identity": IDENTITY})
    missing_code, missing = server.handle_plugin(
        f"{CREATE_PATH}/lost-run", "GET", AUTH, {"identity": {**IDENTITY, "runId": "lost-run"}}
    )
    assert code == 200
    assert status["status"] == "failed"
    assert status["error"] == "RuntimeError"
    assert missing_code == 404
    assert missing == {"error": "run_not_found"}


def test_stop_interrupts_an_attached_agent_and_a_queued_stop_never_runs(home, monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202

    class Agent:
        def __init__(self):
            self.interrupts = []

        def interrupt(self, **kwargs):
            self.interrupts.append(kwargs)

    agent = Agent()
    turn.attach_agent(RUN, agent)
    code, stopped = server.handle_plugin(f"{RUN_PATH}/stop", "POST", AUTH, {"identity": IDENTITY})
    assert code == 202
    assert stopped == {"run_id": RUN, "status": "stopping", "session_id": SESSION}
    assert agent.interrupts == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]

    turn.RUNS.clear()
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    assert server.handle_plugin(f"{RUN_PATH}/stop", "POST", AUTH, {"identity": IDENTITY})[0] == 202

    called = []
    monkeypatch.setattr(turn, "_run_native_paperclip", lambda *args: called.append(args))
    created[-1].run_now()
    _, final = server.handle_plugin(RUN_PATH, "GET", AUTH, {"identity": IDENTITY})
    assert called == []
    assert final["status"] == "cancelled"


@pytest.mark.parametrize("token", ["grant-jwt", None])
def test_create_writes_the_turn_context_with_the_local_session_grant_when_sent(home, monkeypatch, token):
    (home / "profiles" / "agent-1").mkdir(parents=True)
    threading, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", threading)
    for name in ("restore_profile", "save_profile", "report_usage"):
        monkeypatch.setattr(turn, name, lambda *a, **k: None)
    for name in ("restore_root_auth", "save_root_auth", "ensure_profile"):
        monkeypatch.setattr(turn.sync, name, lambda *a, **k: None)
    seen = {}

    class Agent:
        model, session_input_tokens, session_output_tokens = "m", 0, 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            path = turn._turn_context_path(turn._profile_dir("agent-1"), SESSION)
            seen["ctx"] = json.loads(path.read_text()) if path.exists() else None
            return {"final_response": "ok", "failed": False, "completed": True}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    headers = {**AUTH, **({"x-lightbulb-local-session-token": token} if token else {})}

    assert server.handle_plugin(CREATE_PATH, "POST", headers, BODY)[0] == 202
    created[0].run_now()

    # Every Paperclip run gets a context file, so send_room_message works without a grant.
    assert seen["ctx"]["localSessionToken"] == token
    assert seen["ctx"]["family"] == "paperclip" and seen["ctx"]["sessionKey"] == SESSION
    assert not turn._turn_context_path(turn._profile_dir("agent-1"), SESSION).exists()
