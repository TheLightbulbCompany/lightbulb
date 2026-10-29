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
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda method, url, token: seen.update(
        method=method, url=url, token=token) or {"sessions": []})
    lightbulb._local_sessions({"action": "resources"}, session_id=SESSION)
    assert seen == {"method": "GET", "token": "jwt",
                    "url": "https://api.invalid/api/v1/messaging/local-session-agent/resources"}


def test_read_clamps_limit_and_rejects_a_non_uuid_session_id(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_call_sessions",
                        lambda method, url, token: seen.update(url=url) or {})
    lightbulb._local_sessions({"action": "read", "sessionId": SID, "afterSeq": 4, "limit": 999},
                              session_id=SESSION)
    assert seen["url"].endswith(f"/sessions/{SID}/events?afterSeq=4&limit=100")
    assert "Invalid sessionId" in lightbulb._local_sessions({"action": "read", "sessionId": "nope"},
                                                            session_id=SESSION)


def test_local_sessions_tool_has_only_resources_and_read(tmp_path, monkeypatch):
    # Friday reads shared sessions here; she messages one with lightbulb_network.
    params = lightbulb.SESSIONS_SCHEMA["parameters"]
    assert params["properties"]["action"]["enum"] == ["resources", "read"]
    assert set(params["properties"]) == {"action", "sessionId", "afterSeq", "limit"}
    assert "kind" not in params["properties"]
    _ctx(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda *a, **k: called.append(a) or {})
    for action in ("send", "receipt"):
        out = lightbulb._local_sessions({"action": action, "sessionId": SID}, session_id=SESSION)
        assert out == "action must be one of resources|read."
    assert called == []


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
