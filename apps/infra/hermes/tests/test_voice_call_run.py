from types import SimpleNamespace

from bridge import server, turn

AUTH = {"authorization": "Bearer gw-test"}
SESSION = "agent:agent-1:lightbulb:call:call-1"
IDENTITY = {"runId": "run-1", "companyId": "company-1", "agentId": "agent-1", "callId": "call-1", "sessionId": SESSION}
REQUEST = {"input": "check payouts", "instructions": "speak briefly", "session_id": SESSION,
           "frames": {"endpoint": "https://e.invalid", "channel": "/ws/w/users/k"}}
BODY = {"identity": IDENTITY, "request": REQUEST}


def test_envelope_accepts_a_call():
    assert server._call_envelope(BODY, require_request=True) is not None


def test_envelope_rejects_a_session_key_for_another_call():
    assert server._call_envelope({**BODY, "identity": {**IDENTITY, "callId": "call-2"}}, require_request=True) is None


def test_envelope_rejects_a_paperclip_session_key():
    pc = "paperclip:company:company-1:agent:agent-1:run:run-1"
    assert server._call_envelope({"identity": {**IDENTITY, "sessionId": pc}, "request": {**REQUEST, "session_id": pc}}, require_request=True) is None


def test_create_registers_and_spawns(monkeypatch):
    spawned = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append((target, args))))
    status, body = server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert (status, body["status"]) == (202, "queued")
    assert spawned[0][0] is turn.run_call_turn
    assert turn.RUNS["run-1"]["kind"] == "call"


def test_stop_aborts_a_live_call_run(monkeypatch):
    turn.register_call_run(IDENTITY)
    status, body = server.handle_plugin("/internal/calls/v1/runs/run-1/stop", "POST", AUTH, {"identity": IDENTITY})
    assert (status, body["status"]) == (202, "stopping")
    assert turn.RUNS["run-1"]["aborted"] is True


def test_stop_unknown_run_is_404():
    status, _ = server.handle_plugin("/internal/calls/v1/runs/nope/stop", "POST", AUTH, {"identity": {**IDENTITY, "runId": "nope"}})
    assert status == 404


def test_call_frames_use_the_call_envelope():
    calls = []
    pub = turn.CallFramePublisher("https://e.invalid", "/ws/w/users/k", "call-1", "agent-1", "run-1",
                                  publish=lambda _e, channel, *events: calls.append((channel, events)) or 200)
    pub.tool_start("tc-1", "browser", {})
    channel, events = calls[-1]
    assert channel == "/ws/w/users/k"
    assert events[0]["type"] == "call.turn.frame"
    assert events[0]["payload"]["callId"] == "call-1" and events[0]["payload"]["turnId"] == "run-1"
    assert events[0]["payload"]["frame"]["type"] == "tool_start"
    assert "roomId" not in events[0]


def test_call_turn_gets_the_dm_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    real_scope = turn._secret_scope

    def secret_scope(agent_id, overlay=None):
        seen["overlay"] = overlay
        return real_scope(agent_id, overlay)

    def begin_turn(run_id, agent_id, sink=None, family="room", room_id=None):
        seen["family"] = family
        return {}

    def construct(agent_id, body, frames, session_key):
        seen["frames"], seen["session"] = frames, session_key
        return SimpleNamespace(run_conversation=lambda **_kw: {"api_calls": 1, "final_response": "It landed."})

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)
    monkeypatch.setattr(turn.browser_control, "begin_turn", begin_turn)
    monkeypatch.setattr(turn.browser_control, "release", lambda _l: None)
    monkeypatch.setattr(turn, "_construct_agent", construct)
    turn.register_call_run(IDENTITY)
    frames = turn.CallFramePublisher("", "", "call-1", "agent-1", "run-1")
    assert turn._run_call(IDENTITY, REQUEST, frames)[0::2] == ("completed", "It landed.")
    assert seen["overlay"] is None  # no PAPERCLIP_* variables
    assert seen["family"] == "call"
    assert seen["session"] == SESSION and seen["frames"] is frames


def _no_thread(monkeypatch, spawned):
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append(args)))


def test_replay_is_200_with_one_spawn_and_no_busy_leak(monkeypatch):
    spawned = []
    _no_thread(monkeypatch, spawned)
    base = turn._busy
    assert server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)[0] == 202
    status, body = server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert (status, body["status"]) == (200, "replayed")
    assert len(spawned) == 1
    assert turn._busy == base + 1  # only the first reservation is held (released by the turn)
    turn._busy_add(-1)


def test_thread_start_failure_unregisters_and_releases(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=boom))
    base = turn._busy
    try:
        server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    except RuntimeError:
        pass
    assert "run-1" not in turn.RUNS
    assert turn._busy == base


def test_run_call_turn_closes_the_run_and_accounts(monkeypatch):
    saved, published = [], []
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn, "restore_profile", lambda _a: None)
    monkeypatch.setattr(turn, "save_profile", lambda a: saved.append(a))
    monkeypatch.setattr(turn, "report_usage", lambda *_a: True)
    monkeypatch.setattr(turn, "_await_post_turn_writes", lambda _a: None)
    monkeypatch.setattr(turn, "_run_call", lambda *_a: ("completed", None, "ok", dict(turn.ZERO_USAGE)))
    monkeypatch.setattr(turn, "publish_frame", lambda _e, _c, *events: published.extend(events) or 200)
    turn.register_call_run(IDENTITY)
    base = turn._busy
    turn.run_call_turn(IDENTITY, REQUEST)
    assert turn.RUNS["run-1"]["state"] == "ended" and turn.RUNS["run-1"]["outcome"] == "completed"
    assert turn._busy == base and saved == ["agent-1"]
    frames = [e["payload"]["frame"] for e in published]
    assert {"type": "run_ended", "outcome": "done"}.items() <= next(f for f in frames if f["type"] == "run_ended").items()
    assert frames[-1]["type"] == "run_active" and frames[-1]["active"] is False


def test_aborted_while_queued_never_enters_the_context_file(monkeypatch):
    entered = []
    monkeypatch.setattr(turn, "_turn_context_file", lambda *_a: entered.append(1) or (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(turn, "_construct_agent", lambda *_a: (_ for _ in ()).throw(AssertionError))
    turn.register_call_run(IDENTITY)
    turn.abort_run("run-1")
    frames = turn.CallFramePublisher("", "", "call-1", "agent-1", "run-1")
    assert turn._run_call(IDENTITY, REQUEST, frames)[0] == "aborted"
    assert entered == []
