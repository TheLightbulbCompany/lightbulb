"""lightbulb_workspace: the backend's agent-act route, with the turn's grant."""
import io
import json
import pathlib
import sys
import urllib.error
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:ceo:lightbulb:channel:r"
ROOM = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def turn(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {
        "apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
        "runId": "22222222-2222-4222-8222-222222222222", "eventId": "e", "localSessionToken": "jwt",
        "sessionKey": SESSION, "roomId": ROOM})
    sent = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.append((url, token, body)) or {"ok": True})
    return sent


def test_no_grant_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    assert lightbulb._workspace({"action": "leave_room"}, session_id=SESSION) == lightbulb.NO_TURN


def test_posts_the_action_with_the_turns_grant(turn):
    out = lightbulb._workspace({"action": "pin", "roomId": ROOM, "args": {"message_id": "m1"}}, session_id=SESSION)
    assert json.loads(out) == {"ok": True}
    assert turn == [("https://api.invalid/api/v1/messaging/agent-act", "jwt",
                     {"action": "pin", "roomId": ROOM, "args": {"message_id": "m1"}})]


def test_a_refusal_passes_the_backends_message_through(turn, monkeypatch):
    body = json.dumps({"code": "ROOM_FORBIDDEN", "message": "You cannot change that room."}).encode()

    def refused(*_a):
        raise urllib.error.HTTPError("u", 403, "Forbidden", None, io.BytesIO(body))

    monkeypatch.setattr(lightbulb, "_post", refused)
    out = lightbulb._workspace({"action": "update_room", "roomId": ROOM, "args": {}}, session_id=SESSION)
    assert "ROOM_FORBIDDEN" in out and "You cannot change that room." in out


def test_create_room_gets_a_stable_client_id_unless_given(turn):
    lightbulb._workspace({"action": "create_room", "args": {"name": "a"}}, session_id=SESSION)
    lightbulb._workspace({"action": "create_room", "args": {"name": "b", "clientRoomId": "mine"}}, session_id=SESSION)
    first, second = (b["args"] for _, _, b in turn)
    assert str(uuid.UUID(first["clientRoomId"])) == first["clientRoomId"]
    assert second["clientRoomId"] == "mine"
