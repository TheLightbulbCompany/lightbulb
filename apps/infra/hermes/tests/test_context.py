import json
from bridge import turn
from tests import factories as f

ROOT = "55555555-5555-4555-8555-555555555555"


def test_context_carries_the_message_id_and_thread_root():
    body = f.ingress(message=f.message(threadRootMessageId=ROOT))
    ctx = turn.room_context(body, "ceo")
    assert ctx["message"]["id"] == body["message"]["messageId"]
    assert ctx["message"]["threadRootMessageId"] == ROOT
    assert ctx["eventKind"] == "user_request"


def test_context_carries_sessions_participants_and_mentions_verbatim():
    sessions = [{"sessionId": f.ROOM, "humanUserId": "user_1", "displayName": "Prez",
                 "provider": "claude", "cwdName": "lightbulb", "title": None,
                 "status": "idle", "latestEventSeq": 4, "lastEventAt": None}]
    ctx = turn.room_context(f.ingress(sessions=sessions), "ceo")
    assert ctx["sessions"] == sessions
    assert {p["id"] for p in ctx["participants"]} == {"user_1", "ceo"}
    assert ctx["mentions"] == []


def test_the_system_prompt_embeds_the_context_as_parseable_json():
    prompt = turn.system_prompt(f.ingress(), "ceo")
    block = prompt.split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(block)["room"]["name"] == "general"
    assert "CEO" in prompt
