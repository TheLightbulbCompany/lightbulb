"""Task 11: Tests for the lightbulb plugin and send_room_message tool."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "room:ceo"


def ctx(tmp_path, **over):
    base = {"apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h",
            "roomId": "11111111-1111-4111-8111-111111111111", "agentId": "ceo",
            "runId": "22222222-2222-4222-8222-222222222222", "eventId": "e",
            "threadRootMessageId": "55555555-5555-4555-8555-555555555555",
            "localSessionToken": "jwt", "sessionKey": "agent:ceo:lightbulb:channel:r"}
    base.update(over)
    turn_context.write(tmp_path, SESSION, base)
    return base


def test_context_round_trips_by_session_id_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    written = ctx(tmp_path)
    assert turn_context.read(SESSION) == written
    turn_context.clear(tmp_path, SESSION)
    assert turn_context.read(SESSION) is None


def test_send_room_message_defaults_to_the_turns_thread_root(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    expected = ctx(tmp_path)
    sent = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.update(
        url=url, token=token, body=body) or {"messageId": "m1", "roomId": expected["roomId"],
                                             "createdAt": "2026-09-16T10:00:00Z"})
    out = lightbulb._send_room_message(
        {"roomId": f"lightbulb:{expected['roomId']}", "text": "following up"}, session_id=SESSION)
    assert sent["url"] == "https://api.invalid/api/v1/internal/messaging/agent-messages/org_test"
    assert sent["token"] == "h"
    assert sent["body"]["threadRootMessageId"] == expected["threadRootMessageId"]
    assert sent["body"]["roomId"] == expected["roomId"] and sent["body"]["runId"] == expected["runId"]
    assert json.loads(out)["messageId"] == "m1"


def test_an_explicit_thread_root_wins_and_no_context_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: body)
    out = lightbulb._send_room_message(
        {"roomId": "11111111-1111-4111-8111-111111111111", "text": "x",
         "threadRootMessageId": "abc"}, session_id=SESSION)
    assert "abc" in out
    assert "no active Lightbulb turn" in lightbulb._send_room_message(
        {"roomId": "11111111-1111-4111-8111-111111111111", "text": "x"}, session_id="other")


def test_a_non_uuid_room_is_refused_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: (_ for _ in ()).throw(AssertionError("no call")))
    assert "Invalid roomId" in lightbulb._send_room_message({"roomId": "general", "text": "x"},
                                                            session_id=SESSION)


# Additional tests for comprehensive coverage
def test_send_room_message_normalizes_room_id(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    seen = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: (
        seen.update(body=body), {})[1])
    lightbulb._send_room_message(
        {"roomId": "11111111-1111-4111-8111-111111111111".upper(), "text": "test"},
        session_id=SESSION)
    assert seen["body"]["roomId"] == "11111111-1111-4111-8111-111111111111"


def test_send_room_message_requires_text(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    result = lightbulb._send_room_message({"roomId": "11111111-1111-4111-8111-111111111111", "text": ""},
                                          session_id=SESSION)
    assert "text is required" in result
