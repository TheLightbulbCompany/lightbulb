"""Task 14: Tests for the lightbulb_local_sessions tool."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402


SESSION = "room:ceo"
SID = "66666666-6666-4666-8666-666666666666"


def _ctx(tmp_path, monkeypatch, token="jwt"):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {
        "apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h",
        "roomId": "11111111-1111-4111-8111-111111111111", "agentId": "ceo", "runId": "r",
        "eventId": "e", "threadRootMessageId": None, "localSessionToken": token,
        "sessionKey": "agent:ceo:lightbulb:channel:r"})


def test_resources_uses_the_grant_jwt_as_the_bearer(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda method, url, token, body=None: seen.update(
        method=method, url=url, token=token) or {"sessions": []})
    lightbulb._local_sessions({"action": "resources"}, session_id=SESSION)
    assert seen == {"method": "GET", "token": "jwt",
                    "url": "https://api.invalid/api/v1/messaging/local-session-agent/resources"}


def test_read_clamps_limit_and_rejects_a_non_uuid_session_id(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_call_sessions",
                        lambda method, url, token, body=None: seen.update(url=url) or {})
    lightbulb._local_sessions({"action": "read", "sessionId": SID, "afterSeq": 4, "limit": 999},
                              session_id=SESSION)
    assert seen["url"].endswith(f"/sessions/{SID}/events?afterSeq=4&limit=100")
    assert "Invalid sessionId" in lightbulb._local_sessions({"action": "read", "sessionId": "nope"},
                                                            session_id=SESSION)


def test_an_interrupt_carries_no_text_and_send_requires_expected_generation(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_call_sessions",
                        lambda method, url, token, body=None: seen.update(body=body) or {})
    lightbulb._local_sessions({"action": "send", "sessionId": SID, "requestId": "req-1",
                               "expectedGeneration": "g1", "text": "hi", "kind": "interrupt"},
                              session_id=SESSION)
    assert seen["body"] == {"requestId": "req-1", "expectedGeneration": "g1", "kind": "interrupt"}
    assert "expectedGeneration" in lightbulb._local_sessions(
        {"action": "send", "sessionId": SID, "requestId": "r", "text": "hi"}, session_id=SESSION)


def test_an_ask_comes_back_as_awaiting_approval_and_the_ask_bounds_name_their_code(tmp_path, monkeypatch):
    import io
    import urllib.error

    _ctx(tmp_path, monkeypatch)
    send = {"action": "send", "sessionId": SID, "requestId": "req-1", "expectedGeneration": "g1", "text": "hi"}
    ask = {"state": "awaiting_approval", "requestId": "req-1", "expiresAt": 1}
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda *a, **k: ask)
    assert json.loads(lightbulb._local_sessions(send, session_id=SESSION)) == ask
    for status, code in ((429, "LOCAL_SESSION_ASK_PENDING"), (409, "LOCAL_SESSION_ASK_DECLINED"),
                         (403, "LOCAL_SESSION_FORBIDDEN")):
        err = urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(json.dumps({"code": code}).encode()))
        monkeypatch.setattr(lightbulb, "_call_sessions", lambda *a, _e=err, **k: (_ for _ in ()).throw(_e))
        out = lightbulb._local_sessions(send, session_id=SESSION)
        assert out.startswith(code) if code in lightbulb.ASK_REFUSALS else out == lightbulb.SESSIONS_UNAVAILABLE


def test_send_room_message_stays_off_in_a_paperclip_run(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {"family": "paperclip", "localSessionToken": "jwt"})
    assert lightbulb._send_room_message({"roomId": "x", "text": "hi"}, session_id=SESSION) == lightbulb.NO_TURN


def test_an_extra_param_for_the_action_is_refused(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    assert "not allowed" in lightbulb._local_sessions({"action": "resources", "sessionId": SID},
                                                       session_id=SESSION)


def test_every_transport_failure_collapses_to_one_generic_message(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(lightbulb, "_call_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 503 from ...")))
    out = lightbulb._local_sessions({"action": "resources"}, session_id=SESSION)
    assert out == lightbulb.SESSIONS_UNAVAILABLE and "503" not in out
