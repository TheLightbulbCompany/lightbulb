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


def test_registered_under_the_lightbulb_toolset():
    """`register()` imports Hermes itself, so the registration is pinned on the source like the other tools."""
    src = pathlib.Path(lightbulb.__file__).read_text()
    assert 'ctx.register_tool("start_work", "lightbulb", START_WORK_SCHEMA, _start_work' in src


def test_call_turn_context_carries_super_agent_and_call_id():
    identity = {"agentId": "ceo", "runId": RUN, "roomId": ROOM, "callId": "win-1"}
    ctx = turn.call_turn_context(identity, {"superAgent": True})
    assert ctx["superAgent"] is True and ctx["callId"] == "win-1" and ctx["voiceRoomId"] == ROOM
    assert turn.call_turn_context(identity, {})["superAgent"] is False
    assert turn.call_turn_context(identity)["superAgent"] is False  # today's callers pass no request
