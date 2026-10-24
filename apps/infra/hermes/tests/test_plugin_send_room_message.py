"""A voice run never posts into its Live room, whatever thread it names."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:ceo:lightbulb:channel:r"
ROOM = "11111111-1111-4111-8111-111111111111"
OTHER_ROOM = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {
        "apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
        "runId": "22222222-2222-4222-8222-222222222222", "sessionKey": SESSION,
        "roomId": None, "voiceRoomId": ROOM, "family": "call"})
    sent = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.append(body) or {"messageId": "m"})
    return sent


@pytest.mark.parametrize("args", [
    {"roomId": ROOM, "text": "hi"},
    {"roomId": f"lightbulb:{ROOM.upper()}", "text": "hi"},
    {"roomId": ROOM, "text": "hi", "threadRootMessageId": "55555555-5555-4555-8555-555555555555"},
])
def test_the_voice_room_is_refused_for_any_thread(voice, args):
    assert lightbulb._send_room_message(args, session_id=SESSION) == lightbulb.VOICE_ROOM_REFUSAL
    assert voice == []


def test_another_room_still_posts(voice):
    lightbulb._send_room_message({"roomId": OTHER_ROOM, "text": "hi"}, session_id=SESSION)
    assert [b["roomId"] for b in voice] == [OTHER_ROOM]


def test_a_run_less_room_post_uses_the_network_message_id(tmp_path, monkeypatch):
    session = "agent:ceo:lightbulb:network:t-1"
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, session, {
        "apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
        "runId": None, "eventId": "msg-7", "sessionKey": session, "roomId": None, "family": "unavailable"})
    sent = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.append(body) or {"messageId": "m"})
    lightbulb._send_room_message({"roomId": OTHER_ROOM, "text": "hi"}, session_id=session)
    assert sent[0]["eventId"].startswith("msg-7:ceo:tool:")
