"""ask_session: the tool posts the ask to the backend's hook-token route."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

CTX = {"runId": "run-1", "agentId": "agent-1", "ownerId": "OWNER", "apiUrl": "https://api.example/",
       "hookToken": "tok", "roomId": "room-1", "threadRootMessageId": "msg-root"}


def test_ask_session_posts_to_internal_route(monkeypatch):
    calls = []
    monkeypatch.setattr(turn_context, "read", lambda sid: CTX if sid == "s1" else None)
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"taskId": "t1", "state": "held", "holders": ["user_owner"]})
    out = lightbulb._ask_session({"session_id": "abc", "text": "status?", "intent": "founder asked"}, session_id="s1")
    [(url, token, body)] = calls
    assert url == "https://api.example/api/v1/internal/a2a/OWNER/ask-session"
    assert token == "tok"
    assert body["sessionId"] == "abc" and body["agentId"] == "agent-1" and body["intent"] == "founder asked"
    assert body["originMessageId"] == "msg-root" and body["runId"] == "run-1" and body["roomId"] == "room-1"
    assert json.loads(out)["state"] == "held"
    assert "ask_session" in (pathlib.Path(lightbulb.__file__).parent / "plugin.yaml").read_text()
