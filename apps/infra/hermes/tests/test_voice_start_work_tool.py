"""start_work: offered only in a super-agent Live voice turn; posts the turn's ids, never the model's."""
import io
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

from bridge import turn  # noqa: E402
from tests.test_voice_call_run import env  # noqa: E402, F401 — the _run_call harness

SESSION = "agent:ceo:lightbulb:call:win-1"
RUN = "22222222-2222-4222-8222-222222222222"
ROOM = "11111111-1111-4111-8111-111111111111"


def _ctx(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    base = {"apiUrl": "https://api.invalid/", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
            "runId": RUN, "roomId": None, "voiceRoomId": ROOM, "callId": "win-1", "superAgent": True}
    base.update(over)
    turn_context.write(tmp_path, SESSION, base)


def test_start_work_absent_without_super_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    _ctx(tmp_path, monkeypatch, superAgent=False)
    assert lightbulb._start_work({"title": "t", "brief": "b"}, session_id=SESSION) == lightbulb.START_WORK_ONLY
    _ctx(tmp_path, monkeypatch, voiceRoomId=None, roomId=ROOM)  # a typed room turn, even with the flag
    assert lightbulb._start_work({"title": "t", "brief": "b"}, session_id=SESSION) == lightbulb.START_WORK_ONLY
    assert lightbulb._start_work({"title": "t", "brief": "b"}, session_id="none") == lightbulb.START_WORK_ONLY


def test_start_work_posts_route(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"triggerId": "voice:win-1:" + RUN, "taskId": "t"})
    out = lightbulb._start_work({"title": "Draft the launch mail", "brief": "For Monday.", "specialist": "growth",
                                 "runId": "evil", "roomId": "evil"}, session_id=SESSION)
    assert out == "started"
    assert calls == [("https://api.invalid/api/v1/internal/automations/org_test/voice-start", "h",
                      {"runId": RUN, "callId": "win-1", "roomId": ROOM, "title": "Draft the launch mail",
                       "brief": "For Monday.", "specialist": "growth"})]


def test_start_work_rejects_bad_args_and_reports_refusals(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    assert "error" in json.loads(lightbulb._start_work({"title": "", "brief": "b"}, session_id=SESSION))
    assert "error" in json.loads(lightbulb._start_work({"title": "t"}, session_id=SESSION))

    def refuse(*_a):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"detail":{"code":"no_requester"}}'))

    monkeypatch.setattr(lightbulb, "_post", refuse)
    out = lightbulb._start_work({"title": "t", "brief": "b"}, session_id=SESSION)
    assert out.startswith("Not started")


def test_registered_under_its_own_toolset():
    """`register()` imports Hermes itself, so the registration is pinned on the source like the other tools.
    Its own toolset: the schema must not reach the model on an ordinary voice, room or Worker turn."""
    src = pathlib.Path(lightbulb.__file__).read_text()
    assert 'ctx.register_tool("start_work", VOICE_WORK_TOOLSET, START_WORK_SCHEMA, _start_work' in src
    assert lightbulb.VOICE_WORK_TOOLSET == turn.VOICE_WORK_TOOLSET == "lightbulb_voice_work"


def test_voice_work_toolset_off_every_other_turn(monkeypatch):
    """A profile that lists the toolset still never hands it to a room, task or Router turn (only _run_call
    re-adds it, and only on a super-agent call)."""
    import sys
    import types

    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_voice_work", "memory"], None))
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAIAgent))
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id: {})
    monkeypatch.setattr(turn, "profile_model", lambda c: ("m", "p"))
    monkeypatch.setattr(turn, "_session_db", lambda a: None)
    monkeypatch.setattr(turn, "_credential_pool", lambda *a: None)
    monkeypatch.setattr(turn, "_capture_api_errors", lambda a: None)
    turn._construct_agent("agent-1", {}, None, "agent:agent-1:lightbulb:task:7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
    assert captured["enabled_toolsets"] == ["lightbulb", "memory"]


def test_a_super_agent_call_enables_the_voice_work_toolset(env, home, monkeypatch):  # noqa: F811
    from tests.test_voice_call_run import IDENTITY, REQUEST, _frames

    # a profile that lists the Router's toolset: the call never gets it (only Router runs do)
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_router", "memory"], None))
    turn.register_call_run(IDENTITY)
    assert turn._run_call(IDENTITY, {**REQUEST, "superAgent": True}, _frames())[0] == "completed"
    assert env["limits"]["enabled_toolsets"] == ["lightbulb", "memory", "lightbulb_voice_work"]
    turn.register_call_run(IDENTITY)
    assert turn._run_call(IDENTITY, REQUEST, _frames())[0] == "completed"  # an ordinary call: CALL_RUN_LIMITS only
    assert "enabled_toolsets" not in env["limits"]


def test_call_turn_context_carries_super_agent_and_call_id():
    identity = {"agentId": "ceo", "runId": RUN, "roomId": ROOM, "callId": "win-1"}
    ctx = turn.call_turn_context(identity, {"superAgent": True})
    assert ctx["superAgent"] is True and ctx["callId"] == "win-1" and ctx["voiceRoomId"] == ROOM
    assert turn.call_turn_context(identity, {})["superAgent"] is False
    assert turn.call_turn_context(identity)["superAgent"] is False  # today's callers pass no request
