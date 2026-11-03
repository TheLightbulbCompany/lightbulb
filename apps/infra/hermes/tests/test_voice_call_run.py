import json
import pathlib
import sys
import threading
import types
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from bridge import server, turn

AUTH = {"authorization": "Bearer gw-test"}
ROOM = "abcdef11-1111-4111-8111-111111111111"
SESSION = f"agent:agent-1:lightbulb:channel:{ROOM}"
EP = "https://api.invalid/api/v1/internal/huddles/org_test/rooms/" + ROOM + "/frames"
CALL = "ca11ca11-1111-4111-8111-111111111111"
CALL_SESSION = f"agent:agent-1:lightbulb:call:{CALL}"  # the call's own Hermes transcript
IDENTITY = {"runId": "run-1", "companyId": "company-1", "agentId": "agent-1", "roomId": ROOM,
            "sessionId": SESSION, "callId": CALL, "family": "voice"}
SPEAKER = {"id": "user_1", "name": "Aden"}
REQUEST = {"input": "Aden (voice): check payouts", "instructions": "speak briefly", "session_id": SESSION,
           "frames": {"sink": "backend", "endpoint": EP},
           "voice": {"roomId": ROOM, "roomKind": "channel", "speaker": SPEAKER, "utterance": "check payouts"},
           "recentHistory": [], "participants": [{"kind": "agent", "id": "agent-1", "displayName": "Forge"}]}
BODY = {"identity": IDENTITY, "request": REQUEST}


def _with(identity=None, **request):
    return {"identity": {**IDENTITY, **(identity or {})}, "request": {**REQUEST, **request}}


# ---- envelope ------------------------------------------------------------------------------------
def test_a_voice_identity_on_the_room_key_with_its_call_is_accepted(monkeypatch):
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: None))
    status, body = server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert (status, body["status"]) == (202, "queued")


CALL_KEY = "agent:agent-1:lightbulb:call:call-1"


@pytest.mark.parametrize("body", [
    _with({"sessionId": CALL_KEY}, session_id=CALL_KEY),
    _with(voice={**REQUEST["voice"], "roomId": "22222222-2222-4222-8222-222222222222"}),
    _with({"callId": "call-1", "roomId": None, "sessionId": CALL_KEY}, session_id=CALL_KEY),
    _with(frames={"endpoint": "https://e.invalid", "channel": "/ws/w/users/k"}),
    _with({"family": "call"}),
    _with({"roomId": ROOM.upper(), "sessionId": f"agent:agent-1:lightbulb:channel:{ROOM.upper()}"},
          session_id=f"agent:agent-1:lightbulb:channel:{ROOM.upper()}", voice={**REQUEST["voice"], "roomId": ROOM.upper()}),
    _with(recentHistory=[{"messageId": str(i)} for i in range(51)]),
    {"identity": {k: v for k, v in IDENTITY.items() if k != "callId"}, "request": REQUEST},
    _with({"callId": "call-1"}),
], ids=["call-key", "room-mismatch", "old-callId-shape", "appsync-channel", "not-voice", "uppercase-room", "history-over-50",
        "no-call", "call-not-a-uuid"])
def test_anything_else_is_400(body):
    assert server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, body) == (400, {"error": "invalid_call_run"})


def test_create_registers_and_spawns(monkeypatch):
    spawned = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append((target, args))))
    assert server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)[0] == 202
    assert spawned[0][0] is turn.run_call_turn
    assert turn.RUNS["run-1"]["kind"] == "call" and turn.RUNS["run-1"]["identity"] == IDENTITY


def test_the_backend_grant_rides_into_the_call_turn(monkeypatch):
    spawned = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append(args)))
    assert server.handle_plugin("/internal/calls/v1/runs", "POST", {**AUTH, "X-Lightbulb-Local-Session-Token": "jwt"}, BODY)[0] == 202
    assert spawned[0][0] == {**IDENTITY, "localSessionToken": "jwt"}
    assert turn.RUNS["run-1"]["identity"] == IDENTITY  # never in the identity (Paperclip persists those)
    assert turn.RUNS["run-1"]["grant"] == "jwt"  # the run's current grant: verified_call_room_key reads it


def test_stop_takes_the_same_identity():
    turn.register_call_run(IDENTITY)
    status, body = server.handle_plugin("/internal/calls/v1/runs/run-1/stop", "POST", AUTH, {"identity": IDENTITY})
    assert (status, body["status"]) == (202, "stopping") and turn.RUNS["run-1"]["aborted"] is True
    old = {**IDENTITY, "callId": "call-1", "sessionId": CALL_KEY}
    assert server.handle_plugin("/internal/calls/v1/runs/run-1/stop", "POST", AUTH, {"identity": old})[0] == 400


def test_stop_unknown_run_is_404():
    status, _ = server.handle_plugin("/internal/calls/v1/runs/nope/stop", "POST", AUTH, {"identity": {**IDENTITY, "runId": "nope"}})
    assert status == 404


def test_replay_is_200_with_one_spawn_and_no_busy_leak(monkeypatch):
    spawned = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append(args)))
    base = turn._busy
    assert server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)[0] == 202
    status, body = server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert (status, body["status"]) == (200, "replayed")
    assert len(spawned) == 1 and turn._busy == base + 1
    turn._busy_add(-1)


def test_thread_start_failure_unregisters_and_releases(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=boom))
    base = turn._busy
    with pytest.raises(RuntimeError):
        server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert "run-1" not in turn.RUNS and turn._busy == base


# ---- frames --------------------------------------------------------------------------------------
def _publisher(calls):
    return turn.VoiceFramePublisher(EP, ROOM, "agent-1", "run-1",
                                    publish=lambda _e, _c, *events: calls.extend(events) or 200)


def test_voice_frames_are_voice_run_frames_by_room_run_and_agent():
    calls = []
    _publisher(calls).tool_start("tc-1", "browser", {})
    (event,) = calls
    assert event["type"] == "voice.run.frame" and event["schemaVersion"] == 1
    payload = event["payload"]
    assert (payload["roomId"], payload["runId"], payload["agentId"]) == (ROOM, "run-1", "agent-1")
    assert payload["frame"]["type"] == "tool_start"
    assert (payload["frame"]["runId"], payload["frame"]["agentId"]) == ("run-1", "agent-1")


def test_call_frame_publisher_builds_the_backend_sink_from_the_identity():
    pub = turn.call_frame_publisher(REQUEST["frames"], IDENTITY, "run-1")
    assert isinstance(pub, turn.VoiceFramePublisher)
    assert (pub._endpoint, pub._room_id, pub._agent_id, pub._run_id) == (EP, ROOM, "agent-1", "run-1")


@pytest.mark.parametrize("which", ["room", "slots"])
def test_a_run_that_waits_over_a_second_is_queued_first(monkeypatch, which):
    monkeypatch.setattr(turn, "_turn_slots", threading.BoundedSemaphore(1))
    lock = turn._turn_lock("agent-1", SESSION) if which == "room" else turn._turn_slots
    calls = []
    frames = _publisher(calls)
    lock.acquire()
    threading.Timer(1.3, lock.release).start()
    with turn._waiting_turn_lock("agent-1", SESSION, "run-1", on_wait=lambda: frames.emit({"type": "run_queued"})):
        pass
    assert [e["payload"]["frame"]["type"] for e in calls] == ["run_queued"]


def test_an_uncontended_run_is_never_queued():
    seen = []
    with turn._waiting_turn_lock("agent-1", SESSION, "run-1", on_wait=lambda: seen.append(1)):
        pass
    assert seen == []


# ---- the run -------------------------------------------------------------------------------------
class _Agent(SimpleNamespace):
    session_input_tokens = session_output_tokens = 0
    session_cache_read_tokens = session_cache_write_tokens = 0
    model = "m"


@pytest.fixture
def env(monkeypatch):
    """_run_call with every scope real except Hermes itself: records what the run saw."""
    seen = {"convos": [], "keys": [], "desktop": []}
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    real_scope = turn._secret_scope

    def secret_scope(agent_id, overlay=None):
        seen["overlay"] = overlay
        return real_scope(agent_id, overlay)

    def begin_turn(run_id, agent_id, sink=None, family="room", room_id=None):
        seen["family"] = family
        return {}

    script = []

    def construct(agent_id, body, frames, session_key, **limits):
        seen.update(frames=frames, session=session_key, limits=limits, body=body)

        def run_conversation(**kw):
            seen["convos"].append(kw)
            nxt = script.pop(0) if script else None
            if callable(nxt):
                nxt = nxt()
            return nxt or {"api_calls": 1, "final_response": "It landed."}

        agent = _Agent(run_conversation=run_conversation, _drain_pending_steer=lambda: None)
        seen["agent"] = agent
        return agent

    approval = types.ModuleType("tools.approval_context")
    approval.set_current_session_key = lambda key: seen["keys"].append(key) or "tok"
    approval.reset_current_session_key = lambda tok: seen["keys"].append(("reset", tok))
    monkeypatch.setitem(sys.modules, "tools", sys.modules.get("tools") or types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.approval_context", approval)
    monkeypatch.setattr(turn, "_secret_scope", secret_scope)
    monkeypatch.setattr(turn, "_desktop_active_turn", lambda key, run: seen["desktop"].append((key, run)) or nullcontext())
    monkeypatch.setattr(turn.browser_control, "begin_turn", begin_turn)
    monkeypatch.setattr(turn.browser_control, "release", lambda _l: None)
    monkeypatch.setattr(turn, "_construct_agent", construct)
    seen["script"] = script
    return seen


def _frames():
    return turn.VoiceFramePublisher("", ROOM, "agent-1", "run-1")


def test_a_voice_run_gets_the_dm_environment_on_the_calls_transcript(env, home, monkeypatch):
    """A call is its own conversation: its Hermes session, turn lock and work folder are the call's,
    so the room's chat never waits behind it; async delegations stay bound to the room."""
    monkeypatch.setattr(turn, "with_profile_agents_md", lambda _a, system: f"{system}\n\nAGENTS.md")
    scopes, real_terminal = [], turn._terminal_scope
    monkeypatch.setattr(turn, "_terminal_scope", lambda a, key: scopes.append(key) or real_terminal(a, key))
    locks = []
    env["script"].append(lambda: locks.append((turn._turn_lock("agent-1", SESSION).locked(),
                                               turn._turn_lock("agent-1", CALL_SESSION).locked())))
    turn.register_call_run(IDENTITY)
    assert turn._run_call(IDENTITY, REQUEST, _frames())[0::2] == ("completed", "It landed.")
    assert env["overlay"] == {"LIGHTBULB_ROOM_SESSION_KEY": SESSION, "LIGHTBULB_RUN_ID": IDENTITY["runId"]}
    assert env["family"] == "call"
    assert env["session"] == CALL_SESSION
    assert scopes == [CALL_SESSION] and locks == [(False, True)]  # the call's own work folder and turn lock
    # No step cap, and no post-turn review fork after a spoken turn.
    assert env["limits"] == {"reasoning_config": {"enabled": True, "effort": "low"}, "skip_background_review": True}
    assert env["body"]["room"] == {"id": ROOM, "kind": "channel"} and env["body"]["sender"] == SPEAKER
    assert env["keys"] == [SESSION, ("reset", "tok")]  # compression keeps async delegations attached
    assert env["desktop"] == [(CALL_SESSION, "run-1")]  # found by the session id Hermes hands the plugin
    (convo,) = env["convos"]
    # A typed turn's turn line (sender, live sessions) before what was said.
    assert convo["user_message"] == f"{turn.TURN_CONTEXT_PREFIX}{{\"sender\":{json.dumps(SPEAKER, separators=(',', ':'))},\"sessions\":[],\"eventKind\":\"voice\"}}\nAden (voice): check payouts"
    assert convo["system_message"] is None
    # The room turn's own system prompt (who and where), the spoken-turn note after it, no chat posting rules.
    system = env["agent"].ephemeral_system_prompt
    assert system.startswith("You are ") and "on a Live voice call there" in system
    assert "speak briefly\n\nAGENTS.md" in system and "MEDIA:" not in system and "progress" not in system
    assert f"({turn.conversation_dir('agent-1', CALL_SESSION)})" in system  # the folder its shell really starts in


def test_a_spoken_turn_gets_the_room_block_and_live_sessions_a_typed_turn_gets(env, home):
    """One path (prod Meet 10-09: voice saw no sessions and no room name): what the backend sends a
    typed turn reaches a spoken one -- the room block in the system prompt, the sessions in its turn line."""
    room = {"id": ROOM, "name": "general", "kind": "channel", "revision": 3}
    sessions = [{"sessionId": "s-1", "title": "fix the deploy", "state": "live"}]
    turn.register_call_run(IDENTITY)
    turn._run_call(IDENTITY, {**REQUEST, "room": room, "sessions": sessions}, _frames())
    assert env["body"]["room"] == room and env["body"]["sessions"] == sessions
    (convo,) = env["convos"]
    assert '"sessions":[{"sessionId":"s-1"' in convo["user_message"]
    assert "#general, on a Live voice call there" in env["agent"].ephemeral_system_prompt


def test_a_spoken_turn_starts_the_mac_tools_turn_like_a_typed_one(home, monkeypatch):
    """lightbulb_desktop answered "Turn stopped" on every call: nothing started its run state. Now
    run_call_turn starts it (family "call", keyed by the call's session) and removes it after."""
    seen = {}

    def fake_call(identity, request, frames):
        state = json.loads(turn._desktop_state_path(turn._profile_dir("agent-1"), "run-1").read_text())
        seen.update(state["ctx"])
        return "completed", None, "Done.", dict(turn.ZERO_USAGE)

    monkeypatch.setattr(turn, "_run_call", fake_call)
    monkeypatch.setattr(turn, "call_frame_publisher", lambda spec, identity, run_id: _frames())
    for name in ("finish_run", "report_usage", "save_profile", "restore_profile", "_await_post_turn_writes", "_end_turn"):
        monkeypatch.setattr(turn, name, lambda *a, **k: None)
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    turn.register_call_run(IDENTITY)
    turn.run_call_turn(IDENTITY, REQUEST, reserved=True)
    assert seen["family"] == "call" and seen["sessionKey"] == CALL_SESSION and seen["runId"] == "run-1"
    assert not turn._desktop_state_path(turn._profile_dir("agent-1"), "run-1").exists()


def test_the_turn_context_names_the_voice_room(env, home, monkeypatch):
    seen = []
    monkeypatch.setattr(turn, "_turn_context_file", lambda _a, ctx: seen.append(ctx) or nullcontext())
    turn.register_call_run(IDENTITY)
    turn._run_call(IDENTITY, REQUEST, _frames())
    assert seen[0]["roomId"] is None and seen[0]["voiceRoomId"] == ROOM and seen[0]["family"] == "call"
    assert seen[0]["sessionKey"] == CALL_SESSION  # the plugin reads the file by the agent's session id


def test_the_sessions_tool_finds_the_grant_in_a_call_turn(env, home, monkeypatch):
    """Prod 10-09: lightbulb_local_sessions answered NO_TURN in every voice turn (no grant reached it)."""
    plugins = str(pathlib.Path(__file__).resolve().parents[1] / "plugins")
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
    import lightbulb
    from lightbulb import turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: turn._profile_dir("agent-1"))
    seen = {}
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda method, url, token, body=None: seen.update(
        token=token) or {"sessions": []})
    env["script"].append(lambda: seen.update(out=lightbulb._local_sessions({"action": "resources"}, session_id=CALL_SESSION)))
    turn.register_call_run(IDENTITY)
    assert turn._run_call({**IDENTITY, "localSessionToken": "jwt"}, REQUEST, _frames())[0] == "completed"
    assert seen["token"] == "jwt" and seen["out"] != lightbulb.NO_TURN
    assert turn_context.read(CALL_SESSION) is None  # cleared with the turn


def _sessions_tool(monkeypatch):
    """The real plugin tool, recording the grant each call sends."""
    plugins = str(pathlib.Path(__file__).resolve().parents[1] / "plugins")
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
    import lightbulb
    from lightbulb import turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: turn._profile_dir("agent-1"))
    tokens = []
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda method, url, token, body=None: tokens.append(token) or {"sessions": []})
    return lambda: lightbulb._local_sessions({"action": "resources"}, session_id=CALL_SESSION), tokens


def _steer(grant=None):
    return turn.steer_run("run-1", "Guest (voice): x", grant=grant)


def test_the_steer_route_hands_the_lowered_grant_to_the_run(monkeypatch):
    seen = []
    monkeypatch.setattr(turn, "steer_run", lambda run_id, text, grant=None: seen.append(grant) or True)
    path = f"/plugins/lightbulb-channel/runs/{CALL}/steer"
    server.handle_plugin(path, "POST", {**AUTH, "X-Lightbulb-Local-Session-Token": "lowered"}, {"text": "x"})
    server.handle_plugin(path, "POST", AUTH, {"text": "x"})
    assert seen == ["lowered", None]


@pytest.mark.parametrize("when", ["queued", "running"])
def test_a_steer_by_another_speaker_lowers_the_grant_for_the_rest_of_the_run(env, home, monkeypatch, when):
    """P1: a guest's steer must not borrow the asker's direct access to their own sessions."""
    tool, tokens = _sessions_tool(monkeypatch)
    turn.register_call_run(IDENTITY)
    if when == "queued":
        assert _steer("lowered") is True

    def mid_run():
        env["agent"].steer = lambda text: True
        tool()  # before the steer: the asker's grant
        if when == "running":
            assert _steer("lowered") is True
        tool()
        assert _steer() is True  # a later steer without a grant (the same speaker) never raises it
        tool()

    env["script"].append(mid_run)
    assert turn._run_call({**IDENTITY, "localSessionToken": "jwt"}, REQUEST, _frames())[0] == "completed"
    assert tokens == (["lowered"] * 3 if when == "queued" else ["jwt", "lowered", "lowered"])
    assert not turn._turn_context_path(turn._profile_dir("agent-1"), CALL_SESSION).exists()


def test_a_steer_by_the_same_speaker_keeps_the_grant(env, home, monkeypatch):
    tool, tokens = _sessions_tool(monkeypatch)
    turn.register_call_run(IDENTITY)

    def mid_run():
        env["agent"].steer = lambda text: True
        assert _steer() is True
        tool()

    env["script"].append(mid_run)
    turn._run_call({**IDENTITY, "localSessionToken": "jwt"}, REQUEST, _frames())
    assert tokens == ["jwt"]


def test_a_steer_after_the_turn_never_recreates_its_context_file(env, home):
    turn.register_call_run(IDENTITY)
    turn._run_call({**IDENTITY, "localSessionToken": "jwt"}, REQUEST, _frames())
    turn.RUNS["run-1"]["agent"].steer = lambda text: True
    turn.steer_run("run-1", "Guest (voice): x", grant="lowered")  # still "running" until finish_run
    assert not turn._turn_context_path(turn._profile_dir("agent-1"), CALL_SESSION).exists()


def test_a_steer_that_lands_before_the_agent_rides_the_user_message(env, home):
    turn.register_call_run(IDENTITY)
    assert turn.steer_run("run-1", "Aden (voice): also X") is True
    turn._run_call(IDENTITY, REQUEST, _frames())
    assert env["convos"][0]["user_message"].endswith("\nAden (voice): also X")


def test_a_late_steer_is_answered_in_the_same_run(env, home):
    env["script"].extend([{"api_calls": 1, "final_response": "First.", "pending_steer": "Aden (voice): and Y",
                           "messages": [{"role": "user", "content": "x"}]},
                          {"api_calls": 1, "final_response": "Second."}])
    turn.register_call_run(IDENTITY)
    assert turn._run_call(IDENTITY, REQUEST, _frames())[2] == "First.\n\nSecond."
    assert env["convos"][1]["user_message"] == "Aden (voice): and Y"


def test_aborted_while_queued_never_enters_the_context_file(monkeypatch):
    entered = []
    monkeypatch.setattr(turn, "_turn_context_file", lambda *_a: entered.append(1) or (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(turn, "_construct_agent", lambda *_a: (_ for _ in ()).throw(AssertionError))
    turn.register_call_run(IDENTITY)
    turn.abort_run("run-1")
    assert turn._run_call(IDENTITY, REQUEST, _frames())[0] == "aborted"
    assert entered == []


class _Res:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _closing(monkeypatch):
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn, "restore_profile", lambda _a: None)
    monkeypatch.setattr(turn, "_await_post_turn_writes", lambda _a: None)


def test_run_call_turn_starts_closes_and_reports_voice_usage(monkeypatch, posted):
    saved, published, started = [], [], []
    _closing(monkeypatch)
    monkeypatch.setattr(turn, "save_profile", lambda a: saved.append(a))
    monkeypatch.setattr(turn, "_run_call", lambda *_a: ("completed", None, "ok", dict(turn.ZERO_USAGE)))
    real_started = turn.VoiceFramePublisher.started
    monkeypatch.setattr(turn.VoiceFramePublisher, "started", lambda self: started.append(self._room_id) or real_started(self))
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda req, timeout: published.append(req) or _Res())
    turn.register_call_run(IDENTITY)
    base = turn._busy
    turn.run_call_turn(IDENTITY, REQUEST)
    assert started == [ROOM]
    assert turn.RUNS["run-1"]["state"] == "ended" and turn.RUNS["run-1"]["outcome"] == "completed"
    assert turn._busy == base and saved == ["agent-1"]
    frames = [e["payload"]["frame"]["type"] for req in published for e in json.loads(req.data)["events"]]
    assert frames == ["chunk", "run_ended"]
    ((path, body),) = posted
    assert path == "usage/org_test" and body["sessionKey"] == SESSION and body["family"] == "voice"


# ---- limits --------------------------------------------------------------------------------------
def _fake_hermes(monkeypatch, built):
    class AIAgent:
        def __init__(self, **kw):
            built.append(kw)

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda _a: {})
    monkeypatch.setattr(turn, "profile_model", lambda _c: ("m", "p"))
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (None, None))
    monkeypatch.setattr(turn, "_capture_api_errors", lambda _a: None)
    monkeypatch.setattr(turn, "_session_db", lambda _a: object())  # a room key needs state.db (#3565)


def test_construct_agent_forwards_call_limits_to_aiagent(monkeypatch):
    built = []
    _fake_hermes(monkeypatch, built)
    turn._construct_agent("agent-1", {}, None, SESSION, **turn.CALL_RUN_LIMITS)
    turn._construct_agent("agent-1", {}, None, SESSION)
    assert built[0]["reasoning_config"] == {"enabled": True, "effort": "low"} and "max_iterations" not in built[0]
    assert "reasoning_config" not in built[1] and "max_iterations" not in built[1]


def test_room_turns_never_pass_call_limits(home, monkeypatch):
    from tests import factories as f

    seen = []
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: seen.append(k) or _Agent(
        run_conversation=lambda **_kw: {"api_calls": 1, "final_response": "ok"}))
    turn._run_hermes("ceo", f.ingress(), None)
    assert seen == [{}]
