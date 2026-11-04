"""ask_session: the tool posts the ask to the backend's hook-token route."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

CTX = {"runId": "run-1", "agentId": "agent-1", "ownerId": "OWNER", "apiUrl": "https://api.example/",
       "hookToken": "tok", "roomId": "room-1", "messageId": "msg-in",
       "threadRootMessageId": "msg-root"}


def test_ask_session_posts_to_internal_route(monkeypatch):
    calls = []
    monkeypatch.setattr(turn_context, "read", lambda sid: CTX if sid == "s1" else None)
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"taskId": "t1", "state": "held", "holders": ["user_owner"]})
    args = {"session_id": "abc", "text": "status?", "intent": "founder asked", "origin_message_id": "agent-pick"}
    out = lightbulb._ask_session(args, session_id="s1")
    [(url, token, body)] = calls
    assert url == "https://api.example/api/v1/internal/a2a/OWNER/ask-session"
    assert token == "tok"
    assert body["sessionId"] == "abc" and body["agentId"] == "agent-1" and body["intent"] == "founder asked"
    assert body["originMessageId"] == "msg-in" and body["runId"] == "run-1" and body["roomId"] == "room-1"
    assert json.loads(out)["state"] == "held"
    assert "ask_session" in (pathlib.Path(lightbulb.__file__).parent / "plugin.yaml").read_text()
    assert "origin_message_id" not in lightbulb.ASK_SESSION_SCHEMA["parameters"]["properties"]


def test_origin_falls_back_to_thread_root_and_needs_a_room(monkeypatch):
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append(body) or {})
    ctx = {k: v for k, v in CTX.items() if k != "messageId"}
    monkeypatch.setattr(turn_context, "read", lambda sid: ctx)
    lightbulb._ask_session({"session_id": "abc", "text": "t", "intent": "i"}, session_id="s1")
    monkeypatch.setattr(turn_context, "read", lambda sid: {**ctx, "roomId": None})
    lightbulb._ask_session({"session_id": "abc", "text": "t", "intent": "i"}, session_id="s1")
    assert [c["originMessageId"] for c in calls] == ["msg-root", None]
