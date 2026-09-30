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
