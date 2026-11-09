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


@pytest.mark.parametrize("thread", [None, "abc"])
def test_a_paperclip_run_posts_to_any_room_as_a_routine_post(tmp_path, monkeypatch, thread):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    own = ctx(tmp_path, family="paperclip")
    sent = {}
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: sent.update(body=body) or {"messageId": "m1"})
    # Its own room too: a Paperclip run's answer is the issue result, so nothing posts twice.
    lightbulb._send_room_message({"roomId": own["roomId"], "text": "brief", "threadRootMessageId": thread},
                                 session_id=SESSION)
    assert "runId" not in sent["body"]  # no room-run to bind to: the backend counts it as hop 1
    assert sent["body"]["threadRootMessageId"] == thread and sent["body"]["roomId"] == own["roomId"]


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


def _recall(monkeypatch, tmp_path=None):
    import types
    calls = []
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path or pathlib.Path("/nonexistent"))

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


ROOM_KEY = "agent:a1:lightbulb:channel:r1"
CALL_KEY = "agent:a1:lightbulb:call:c1"


class _CallRecallDB(_RecallDB):
    """A call's own transcript, its room's (split by compression), and a DM."""
    parents = {CALL_KEY: None, ROOM_KEY: None, "room-child": ROOM_KEY, "dm": None}

    def search_messages(self, **_kw):
        return [{"session_id": s, "id": i} for i, s in enumerate(["dm", CALL_KEY, ROOM_KEY, "room-child"])]


def _grant(requester):
    import base64
    claims = base64.urlsafe_b64encode(json.dumps({"requesterId": requester}).encode()).decode().rstrip("=")
    return f"h.{claims}.sig"


def _call_run(monkeypatch, requester, lowered_to=False):
    """A live call turn in the bridge's own run registry (what the backend sent over the invoke)."""
    from bridge import turn
    monkeypatch.setattr(turn, "RUNS", {})
    identity = {"runId": "run-c", "agentId": "a1", "roomId": "r1", "sessionId": ROOM_KEY, "callId": "c1"}
    turn.register_call_run(identity, _grant(requester))  # as server.py registers it, with the header's grant
    if lowered_to is not False:
        turn.RUNS["run-c"]["grant"] = _grant(lowered_to)  # a steer by another speaker


@pytest.mark.parametrize(("requester", "lowered"), [(None, False), ("user_ana", None)],
                         ids=["meet-guest", "steered-by-another-speaker"])
def test_a_spoken_turn_nobody_verified_asked_keeps_to_the_call(monkeypatch, tmp_path, requester, lowered):
    """Codex P1: a Meet speaker is not the room's member; their turn must not speak its history."""
    _call_run(monkeypatch, requester, lowered)
    tool, calls = _recall(monkeypatch, tmp_path)
    tool.session_search(query="pricing", db=_CallRecallDB(), current_session_id=CALL_KEY)
    assert [r["session_id"] for r in calls[0]["db"].search_messages()] == [CALL_KEY]


def test_an_agent_cannot_widen_recall_by_rewriting_its_turn_context(monkeypatch, tmp_path):
    """Codex P1: the turn context file is the agent's to write; recall never reads it."""
    _call_run(monkeypatch, None)
    turn_context.write(tmp_path, CALL_KEY, {"family": "call", "agentId": "a1", "voiceRoomId": "r1",
                                            "localSessionToken": _grant("user_forged")})
    tool, calls = _recall(monkeypatch, tmp_path)
    tool.session_search(query="pricing", db=_CallRecallDB(), current_session_id=CALL_KEY)
    assert [r["session_id"] for r in calls[0]["db"].search_messages()] == [CALL_KEY]


def test_a_spoken_turn_recalls_its_rooms_history_as_chat_does(monkeypatch, tmp_path):
    """Prod Meet 10-09: on a call, recall stopped at the call's own transcript. A spoken turn a
    signed-in member asked belongs to its room: it reaches the room's lineage too, never another."""
    _call_run(monkeypatch, "user_ana")
    tool, calls = _recall(monkeypatch, tmp_path)
    db = _CallRecallDB()
    assert tool.session_search(query="pricing", db=db, current_session_id=CALL_KEY) == "hermes"
    assert [r["session_id"] for r in calls[0]["db"].search_messages()] == [CALL_KEY, ROOM_KEY, "room-child"]
    assert tool.session_search(session_id="room-child", db=db, current_session_id=CALL_KEY) == "hermes"
    out = tool.session_search(session_id="dm", db=db, current_session_id=CALL_KEY)
    assert json.loads(out)["error"] == lightbulb.RECALL_SCOPE
    # A typed turn's own lineage only, as before.
    calls.clear()
    tool.session_search(query="pricing", db=db, current_session_id="room-child")
    assert [r["session_id"] for r in calls[0]["db"].search_messages()] == [ROOM_KEY, "room-child"]


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


# ---- lightbulb_read_rooms: other rooms with the asker's access ------------------------------------
def test_read_rooms_reads_through_the_turns_grant(tmp_path, monkeypatch):
    """The MCP's find_rooms/read_messages for an agent turn: the turn's grant, never an agent-chosen person."""
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx(tmp_path)
    calls = []
    monkeypatch.setattr(lightbulb, "_call_sessions", lambda method, url, token: calls.append((method, url, token)) or {"rooms": []})
    assert json.loads(lightbulb._read_rooms({"action": "find", "query": "launch"}, session_id=SESSION)) == {"rooms": []}
    lightbulb._read_rooms({"action": "read", "roomId": OTHER_ROOM, "afterSeq": 4}, session_id=SESSION)
    assert calls == [
        ("GET", "https://api.invalid/api/v1/messaging/agent-read/rooms?query=launch&limit=30", "jwt"),
        ("GET", f"https://api.invalid/api/v1/messaging/agent-read/messages?room_id={OTHER_ROOM}&limit=25&after_seq=4", "jwt"),
    ]


def test_read_rooms_refusals_reach_the_agent_as_plain_words(tmp_path, monkeypatch):
    import urllib.error
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    assert lightbulb._read_rooms({"action": "find"}, session_id=SESSION) == lightbulb.NO_TURN  # no turn here
    ctx(tmp_path)
    assert "Invalid roomId" in lightbulb._read_rooms({"action": "read", "roomId": "general"}, session_id=SESSION)

    def refused(*_a):
        raise urllib.error.HTTPError("u", 403, "forbidden", None, None)

    monkeypatch.setattr(lightbulb, "_call_sessions", refused)
    assert lightbulb._read_rooms({"action": "find"}, session_id=SESSION) == lightbulb.READ_ROOMS_REFUSED


def test_session_search_passes_arguments_a_newer_hermes_adds(monkeypatch):
    """Prod 10-09: Hermes's session_search gained `after`; the wrapper's fixed signature raised TypeError."""
    tool, calls = _recall(monkeypatch)
    assert tool.session_search(query="pricing", db=_RecallDB(), current_session_id="child", after="2026-10-01") == "hermes"
    assert calls[0]["after"] == "2026-10-01" and calls[0]["profile"] is None


def test_workspace_tool_retires_derived_search_before_backend_routes():
    assert lightbulb.WORKSPACE_SCHEMA["parameters"]["properties"]["action"]["enum"] == [
        "add_member", "set_listen_mode", "remove_member", "create_room", "update_room", "pin", "unpin",
    ]
    assert "search_messages" not in lightbulb.WORKSPACE_SCHEMA["description"]
    assert "search_sessions" not in lightbulb.WORKSPACE_SCHEMA["description"]
