"""Task 11: Tests for the send_room_message tool."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402


SESSION = "room:ceo"
ROOM_ID = "11111111-1111-4111-8111-111111111111"


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {
        "apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h",
        "roomId": ROOM_ID, "agentId": "ceo", "runId": "r", "eventId": "e",
        "threadRootMessageId": None})


def test_send_room_message_requires_context(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    result = lightbulb._send_room_message({"roomId": ROOM_ID, "text": "hi"}, session_id="missing")
    assert result == lightbulb.NO_TURN


def test_send_room_message_normalizes_room_id(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: (
        seen.update(url=url, body=body), {})[1])
    lightbulb._send_room_message(
        {"roomId": f"lightbulb:{ROOM_ID.upper()}", "text": "test"},
        session_id=SESSION)
    assert seen["body"]["roomId"] == ROOM_ID.lower()


def test_send_room_message_rejects_invalid_room_id(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    result = lightbulb._send_room_message({"roomId": "not-a-uuid", "text": "hi"}, session_id=SESSION)
    assert "Invalid roomId" in result


def test_send_room_message_requires_text(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    result = lightbulb._send_room_message({"roomId": ROOM_ID, "text": ""}, session_id=SESSION)
    assert "text is required" in result


def test_send_room_message_carries_thread_root_or_defaults_to_current(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: (
        seen.update(body=body), {})[1])
    lightbulb._send_room_message(
        {"roomId": ROOM_ID, "text": "hi", "threadRootMessageId": "thread-1"},
        session_id=SESSION)
    assert seen["body"]["threadRootMessageId"] == "thread-1"

    # Omitted thread defaults to None (ctx doesn't have one)
    lightbulb._send_room_message(
        {"roomId": ROOM_ID, "text": "hi"},
        session_id=SESSION)
    assert seen["body"]["threadRootMessageId"] is None


def test_send_room_message_posts_to_internal_messaging_endpoint(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: (
        seen.update(url=url, token=token), {})[1])
    lightbulb._send_room_message({"roomId": ROOM_ID, "text": "hi"}, session_id=SESSION)
    assert seen["url"] == "https://api.invalid/api/v1/internal/messaging/agent-messages/org_test"
    assert seen["token"] == "h"
