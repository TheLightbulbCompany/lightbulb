"""Task 11: Tests for the lightbulb plugin and send_room_message tool."""
import json
import pytest
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "room:ceo"
# Ruling M6-R45 refuses a send into the turn's OWN room+thread, so every test about the send
# path itself (defaulting, normalizing) has to target a room the turn is not answering.
OTHER_ROOM = "99999999-9999-4999-8999-999999999999"


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
        {"roomId": f"lightbulb:{OTHER_ROOM}", "text": "following up"}, session_id=SESSION)
    assert sent["url"] == "https://api.invalid/api/v1/internal/messaging/agent-messages/org_test"
    assert sent["token"] == "h"
    assert sent["body"]["threadRootMessageId"] == expected["threadRootMessageId"]
    assert sent["body"]["roomId"] == OTHER_ROOM and sent["body"]["runId"] == expected["runId"]
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
    lightbulb._send_room_message({"roomId": OTHER_ROOM.upper(), "text": "test"}, session_id=SESSION)
    assert seen["body"]["roomId"] == OTHER_ROOM


def test_send_room_message_requires_text(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    result = lightbulb._send_room_message({"roomId": "11111111-1111-4111-8111-111111111111", "text": ""},
                                          session_id=SESSION)
    assert "text is required" in result


# ---- Ruling M6-R45: the turn's own room+thread is answered by the turn itself ----------------
def _no_post(monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: (_ for _ in ()).throw(AssertionError("no call")))


def test_posting_into_this_turns_own_thread_is_refused_without_a_network_call(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    own = ctx(tmp_path)
    _no_post(monkeypatch)
    for args in ({"roomId": own["roomId"], "text": "hi"},                                    # omitted thread
                 {"roomId": own["roomId"], "text": "hi", "threadRootMessageId": own["threadRootMessageId"]},
                 {"roomId": f"lightbulb:{own['roomId'].upper()}", "text": "hi"}):            # prefixed/uppercased
        assert lightbulb._send_room_message(args, session_id=SESSION) == lightbulb.OWN_THREAD_REFUSAL


def test_a_dm_turn_with_no_thread_root_still_refuses_its_own_room(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    own = ctx(tmp_path, threadRootMessageId=None)
    _no_post(monkeypatch)
    assert lightbulb._send_room_message({"roomId": own["roomId"], "text": "hi"},
                                        session_id=SESSION) == lightbulb.OWN_THREAD_REFUSAL


def test_another_thread_in_the_same_room_and_another_room_still_post(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    own = ctx(tmp_path)
    sent = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.append(body) or {"messageId": "m"})
    lightbulb._send_room_message({"roomId": own["roomId"], "text": "x",
                                  "threadRootMessageId": "another-thread"}, session_id=SESSION)
    lightbulb._send_room_message({"roomId": OTHER_ROOM, "text": "x"}, session_id=SESSION)
    assert [b["roomId"] for b in sent] == [own["roomId"], OTHER_ROOM]


def test_a_refused_file_memory_write_is_logged(caplog):
    full = json.dumps({"success": False, "usage": "2,190/2,200",
                       "error": "Adding this entry (80 chars) would exceed the limit."})
    with caplog.at_level("WARNING"):
        lightbulb._log_memory_full(tool_name="memory", result=full)
        lightbulb._log_memory_full(tool_name="memory", result=json.dumps({"success": True}))
        lightbulb._log_memory_full(tool_name="terminal", result=full)
    assert [r.getMessage() for r in caplog.records] == [
        "hermes file memory full, write refused (2,190/2,200)"]


# ---- session_search: this conversation's own history only ------------------------------------
class _RecallDB:
    """root <- child (a compression split of one room); 'dm' is another conversation."""
    parents = {"child": "root", "root": None, "dm": None}

    def search_messages(self, **_kw):
        return [{"session_id": "dm", "id": 1}, {"session_id": "root", "id": 2}, {"session_id": "child", "id": 3}]

    def resolve_session_by_title(self, title):
        return {"Alice raise": "dm", "Room plan": "root"}.get(title)


def _recall(monkeypatch):
    import types
    calls = []

    def lineage(db, sid):
        while db.parents.get(sid):
            sid = db.parents[sid]
        return sid

    tool = types.SimpleNamespace(
        session_search=lambda **kw: calls.append(kw) or "hermes",
        _resolve_lineage=lineage,
        tool_error=lambda msg, success: json.dumps({"success": success, "error": msg}),
    )
    monkeypatch.setitem(sys.modules, "tools", types.SimpleNamespace(session_search_tool=tool))
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", tool)
    lightbulb._scope_session_search()
    lightbulb._scope_session_search()  # idempotent: a second plugin load does not double-wrap
    return tool, calls


def test_session_search_reaches_only_the_current_conversations_lineage(monkeypatch):
    tool, calls = _recall(monkeypatch)
    db = _RecallDB()
    assert tool.session_search(query="raise", db=db, current_session_id="child") == "hermes"
    (kw,) = calls
    assert kw["profile"] is None
    assert [r["session_id"] for r in kw["db"].search_messages()] == ["root", "child"]  # never the DM
    assert kw["db"].resolve_session_by_title("Alice raise") is None
    assert kw["db"].resolve_session_by_title("Room plan") == "root"
    assert tool.session_search(session_id="root", db=db, current_session_id="child") == "hermes"


@pytest.mark.parametrize("kw", [
    {"session_id": "dm"},                      # another conversation, by id
    {"query": "x", "profile": "engineering"},  # another agent's state.db
    {"session_id": "engineering/dm"},          # the same, as an @session link
    {},                                        # browse: lists every conversation
    {"query": "x", "current_session_id": None},
])
def test_session_search_refuses_anything_outside_it(monkeypatch, kw):
    tool, calls = _recall(monkeypatch)
    out = tool.session_search(**{"db": _RecallDB(), "current_session_id": "child", **kw})
    assert json.loads(out) == {"success": False, "error": lightbulb.RECALL_SCOPE} and calls == []
