"""The session chat door (PR #3807)."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from core.auth import AuthContext
from core.services import messaging, session_chat
from core.services.messaging import ApiProblem

OWNER = "org_o"
ALICE = AuthContext(user_id="user_alice", org_id=OWNER)
BOB = AuthContext(user_id="user_bob", org_id=OWNER)
SID = "11111111-1111-4111-8111-111111111111"
CEO_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ENG_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROOM = session_chat.session_room_id(OWNER, SID)
ROW = {"sessionId": SID, "ownerId": OWNER, "humanUserId": ALICE.user_id, "title": "Fix relay"}
META = {
    "roomId": ROOM,
    "ownerId": OWNER,
    "kind": "channel",
    "name": "Session chat · Fix relay",
    "revision": 1,
    "lastSeq": 0,
    "hidden": True,
    "session": {"sessionId": SID, "ownerId": ALICE.user_id},
}
CEO = {
    "kind": "agent",
    "id": CEO_ID,
    "displayName": "CEO",
    "mentionHandle": "ceo",
    "role": "member",
    "listenMode": "mentions",
}


@pytest.fixture
def env(monkeypatch):
    fakes = {
        "get_room": AsyncMock(return_value=None),
        "create_room": AsyncMock(return_value=META),
        "get_member": AsyncMock(return_value=None),
        "upsert_member": AsyncMock(return_value={**META, "revision": 2}),
        "ensure_user_stream": AsyncMock(return_value="key"),
        "get_user_stream": AsyncMock(return_value={"active": True}),
        "patch_room_meta": AsyncMock(side_effect=lambda room_id, updates: {**META, **updates, "revision": 2}),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(session_chat.messaging_repo, name, fake)
    monkeypatch.setattr(session_chat.local_sessions, "authorize_session", AsyncMock(return_value=(ROW, {})))
    fakes["session"] = AsyncMock(return_value=ROW)  # the session row, read fresh for every lifecycle write
    monkeypatch.setattr(session_chat.local_sessions.repo, "get_session", fakes["session"])
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[CEO]))
    monkeypatch.setattr(
        messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid.removeprefix("user_").title())
    )
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(side_effect=lambda meta, user_id, member=None: meta))
    fakes["close_huddle"] = AsyncMock()
    monkeypatch.setattr(session_chat.livekit_huddles, "close_room", fakes["close_huddle"])
    metrics: list[str] = []
    monkeypatch.setattr(session_chat, "put_metric", lambda name, *a, **k: metrics.append(name))
    fakes["metrics"] = metrics
    return fakes


@pytest.mark.asyncio
async def test_first_open_creates_one_hidden_room_and_seats_the_reader(env):
    await session_chat.open_chat(BOB, SID)
    # Read at the door and again after the seat (a pause in between must not leave a seat).
    assert session_chat.local_sessions.authorize_session.await_args_list == [call(BOB, None, SID)] * 2
    kwargs = env["create_room"].await_args.kwargs
    assert (kwargs["room_id"], kwargs["kind"], kwargs["creator_user_id"]) == (ROOM, "channel", ALICE.user_id)
    assert kwargs["name"] == "Session chat · Fix relay"
    assert kwargs["extra"] == {"hidden": True, "session": {"sessionId": SID, "ownerId": ALICE.user_id}}
    owner, ceo = kwargs["members"]
    assert (owner["kind"], owner["id"], owner["role"]) == ("human", ALICE.user_id, "owner")
    assert ceo == CEO
    seated = env["upsert_member"].await_args.kwargs["member"]
    assert (seated["kind"], seated["id"], seated["role"], seated["listenMode"]) == (
        "human",
        BOB.user_id,
        "member",
        None,
    )
    env["ensure_user_stream"].assert_not_awaited()  # Bob has a stream: seating never touches it


@pytest.mark.asyncio
async def test_a_seated_reader_is_never_seated_twice(env):
    env["get_room"].return_value = META
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    assert await session_chat.open_chat(BOB, SID) == META
    env["create_room"].assert_not_awaited()
    env["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_two_first_opens_at_once_share_the_room(env):
    env["create_room"].side_effect = session_chat.messaging_repo.IdempotencyConflict("raced")
    env["get_room"].side_effect = [None, META]
    await session_chat.open_chat(BOB, SID)
    assert env["upsert_member"].await_args.kwargs["room_id"] == ROOM


@pytest.mark.asyncio
async def test_the_room_follows_the_session_end_and_a_resume(env):
    env["get_room"].return_value = META
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    env["session"].return_value = {**ROW, "endedAt": 10}
    await session_chat.open_chat(BOB, SID)
    updates = env["patch_room_meta"].await_args.args[1]
    assert updates["archivedAt"] and updates["archivedBy"] == "__system__"
    # The same conversation resumed under the same id: the chat takes posts again.
    env["get_room"].return_value = {**META, "archivedAt": "2026-10-07T00:00:00+00:00"}
    env["session"].return_value = ROW
    await session_chat.open_chat(BOB, SID)
    assert env["patch_room_meta"].await_args.args[1] == {"archivedAt": None, "archivedBy": None}


@pytest.mark.asyncio
async def test_a_full_chat_refuses_with_the_vendor_rooms_metric(env):
    env["get_room"].return_value = META
    env["upsert_member"].side_effect = session_chat.messaging_repo.MemberLimitExceeded("full")
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "MEMBER_LIMIT"
    assert env["metrics"] == ["messaging.session_room.capped"]


@pytest.mark.asyncio
async def test_a_session_the_reader_cannot_read_has_no_chat(env):
    session_chat.local_sessions.authorize_session.side_effect = ApiProblem(
        "LOCAL_SESSION_NOT_FOUND", "Local session not found."
    )
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, SID)
    env["create_room"].assert_not_awaited()


def test_the_ceo_is_the_starter_ceo_else_the_legacy_root_else_nobody():
    roster = [
        {"id": CEO_ID, "name": "Friday", "role": "ceo", "reportsTo": None},
        {"id": ENG_ID, "name": "Atlas", "role": "engineer", "reportsTo": CEO_ID},
    ]
    starter = SimpleNamespace(status="active", starter_agent_ids={"ceo": ENG_ID})
    assert [m["id"] for m in session_chat._ceo_member(starter, roster)] == [ENG_ID]
    [ceo] = session_chat._ceo_member(SimpleNamespace(status="active", starter_agent_ids=None), roster)
    assert (ceo["id"], ceo["role"], ceo["listenMode"]) == (CEO_ID, "member", "mentions")
    assert session_chat._ceo_member(SimpleNamespace(status="active", starter_agent_ids=None), roster[1:]) == []
    assert session_chat._ceo_member(SimpleNamespace(status="pending", starter_agent_ids={"ceo": CEO_ID}), roster) == []


def test_a_session_room_projects_its_session_and_nothing_else_does():
    member = {"kind": "human", "id": BOB.user_id}
    fields = messaging._room_summary_fields({**META, "updatedAt": "t", "createdAt": "t"}, member, None)
    assert fields["session"] == messaging.SessionRef(session_id=SID)
    assert messaging.is_session_room(META) and not messaging.is_session_room({"roomId": ROOM})


def test_the_chat_door_never_touches_session_control():
    """Spec decision 3: chat posts through create_human_message only and never grants control."""
    from routers import messaging as router

    source = inspect.getsource(session_chat) + inspect.getsource(router.open_local_session_chat)
    assert "Depends(get_human_session)" in source  # people only: a Clerk session token
    for name in (
        "allowedSenders",
        "allowed_senders",
        "teamControl",
        "team_control",
        "joinRequests",
        "join_requests",
        "submit_command",
        "may_control",
        "request_join",
        "decide_join",
    ):
        assert name not in source, name


@pytest.mark.asyncio
async def test_people_in_a_session_chat_follow_the_session_never_a_hand(monkeypatch):
    with pytest.raises(ApiProblem):
        messaging._deny_vendor_human_change(META, "human")
    messaging._deny_vendor_human_change(META, "agent")  # the owner may add agents
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, {"role": "owner"})))
    for archived in (True, False):
        with pytest.raises(ApiProblem) as refused:
            await messaging.patch_room(ALICE, ROOM, messaging.PatchRoomRequest(archived=archived))
        assert refused.value.error.code == "ROOM_NOT_ARCHIVABLE"
    with pytest.raises(ApiProblem) as refused:
        await messaging.delete_room(ALICE, ROOM)
    assert refused.value.error.code == "ROOM_NOT_DELETABLE"


@pytest.mark.asyncio
async def test_an_agents_unbound_post_stays_in_the_session_chat(monkeypatch):
    from core.services import meeting_invites

    output_room = AsyncMock(return_value="general")
    monkeypatch.setattr(meeting_invites, "output_room", output_room)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    request = messaging.AgentMessageRequest(
        event_id="evt-1",
        room_id=ROOM,
        agent_id=CEO_ID,
        text="hello",
        reply_to_message_id=None,
        thread_root_message_id=None,
    )
    with pytest.raises(ApiProblem) as refused:
        await messaging.create_agent_message(OWNER, request)
    assert refused.value.error.code == "MEMBER_NOT_FOUND"  # judged in the session room itself
    output_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_removes_every_human_but_the_kept_one_with_rotation(monkeypatch):
    members = [
        {"kind": "human", "id": ALICE.user_id},
        {"kind": "human", "id": BOB.user_id},
        {"kind": "agent", "id": CEO_ID},
    ]
    monkeypatch.setattr(session_chat.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(session_chat.messaging_repo, "get_room", AsyncMock(return_value=META))
    remove = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", remove)
    assert await session_chat.silence(OWNER, SID, keep=ALICE.user_id) == 1
    remove.assert_awaited_once_with(META, "human", BOB.user_id)
    remove.reset_mock()
    assert await session_chat.silence(OWNER, SID, keep=None) == 2


@pytest.mark.asyncio
async def test_an_agents_unbound_post_in_a_meet_room_still_goes_to_its_output_room(monkeypatch):
    """The redirect narrowed to rooms with a meeting record: a Google Meet's hidden room keeps it."""
    from core.services import meeting_invites

    meet = {"roomId": ROOM, "ownerId": OWNER, "kind": "channel", "hidden": True, "meeting": {"title": "Standup"}}
    general = {"roomId": "general", "ownerId": OWNER, "kind": "channel"}
    output_room = AsyncMock(return_value="general")
    monkeypatch.setattr(meeting_invites, "output_room", output_room)
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(side_effect=lambda rid: meet if rid == ROOM else general)
    )
    get_member = AsyncMock(
        side_effect=lambda rid, kind, mid: {"kind": "agent", "id": mid} if rid == "general" else None
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=False))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[]))  # stops the post after the redirect
    request = messaging.AgentMessageRequest(
        event_id="evt-2",
        room_id=ROOM,
        agent_id=CEO_ID,
        text="late answer",
        reply_to_message_id=None,
        thread_root_message_id=None,
    )
    with pytest.raises(ApiProblem):
        await messaging.create_agent_message(OWNER, request)
    output_room.assert_awaited_once_with(OWNER, ROOM)
    assert [call.args[0] for call in get_member.await_args_list] == ["general", "general"]  # judged in the output room


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "front", "redirected"), [("in_call", CEO_ID, False), ("ended", CEO_ID, True),
                                                              ("in_call", "someone-else", True)])  # fmt: skip
async def test_lightbulbs_post_stays_in_the_meet_room_only_during_the_call(monkeypatch, status, front, redirected):
    """The meeting's voice says the Router's answers from its room while the call is on; after it, Lightbulb's posts
    go to the organizer's output room again, and another agent's always do."""
    from core.services import meeting_invites

    meet = {"roomId": ROOM, "ownerId": OWNER, "kind": "channel", "hidden": True,
            "meeting": {"title": "Standup", "status": status}}  # fmt: skip
    output_room = AsyncMock(return_value="general")
    monkeypatch.setattr(meeting_invites, "output_room", output_room)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meet))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.task_conversation.front_agent_id", AsyncMock(return_value=front))
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=False))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[]))
    request = messaging.AgentMessageRequest(
        event_id="evt-3", room_id=ROOM, agent_id=CEO_ID, text="We shipped 5095.",
        reply_to_message_id=None, thread_root_message_id=None,
    )  # fmt: skip
    with pytest.raises(ApiProblem):  # nobody seated in these fakes: the post stops after the room is chosen
        await messaging.create_agent_message(OWNER, request)
    assert output_room.await_count == (1 if redirected else 0)


@pytest.mark.asyncio
async def test_a_reader_seated_while_sharing_pauses_loses_the_seat(env, monkeypatch):
    """The pause's silence() may list the room before this seat lands: the door reads the session again."""
    env["get_room"].return_value = META
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    session_chat.local_sessions.authorize_session.side_effect = [(ROW, {}), gone]
    silence = AsyncMock(return_value=1)
    monkeypatch.setattr(session_chat, "silence", silence)
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    env["upsert_member"].assert_awaited_once()
    silence.assert_awaited_once_with(OWNER, SID, keep=ALICE.user_id)


@pytest.mark.asyncio
async def test_chat_state_reads_one_key_per_session_until_the_viewer_has_a_seat(monkeypatch):
    other = "22222222-2222-4222-8222-222222222222"
    pk = session_chat.messaging_repo.room_pk(ROOM)
    seat = session_chat.messaging_repo.member_sk("human", BOB.user_id)
    read = session_chat.messaging_repo.read_sk(BOB.user_id)
    table = {
        (pk, seat): {"pk": pk, "sk": seat, "joinedTopOrdinal": 1},
        (pk, "META"): {**META, "topCount": 4},
        (pk, read): {"topOrdinal": 2},
    }
    batch_get = AsyncMock(side_effect=lambda keys: {key: table[key] for key in keys if key in table})
    monkeypatch.setattr(session_chat.messaging_repo, "batch_get", batch_get)
    assert await session_chat.chat_state(OWNER, BOB.user_id, [SID, other]) == {SID: (ROOM, 2)}
    first, second = (c.args[0] for c in batch_get.await_args_list)
    assert len(first) == 2 and all(sk == seat for _, sk in first)
    assert sorted(second) == sorted([(pk, "META"), (pk, read)])
    batch_get.reset_mock()
    assert await session_chat.chat_state(OWNER, BOB.user_id, [other]) == {}
    assert batch_get.await_count == 1


@pytest.mark.asyncio
async def test_chat_state_is_zero_after_a_read_and_counts_a_later_message_once(monkeypatch):
    """Prod 2026-10-07: the count came back after a reload. The cursor a read writes
    (``_write_cursor``: the seq with its top-level ordinal) is what the count subtracts."""
    repo = session_chat.messaging_repo
    pk, seat = repo.room_pk(ROOM), repo.member_sk("human", BOB.user_id)
    table = {
        (pk, seat): {"pk": pk, "sk": seat, "joinedTopOrdinal": 0},
        (pk, "META"): {**META, "roomId": ROOM, "lastSeq": 3, "topCount": 3},
    }
    monkeypatch.setattr(
        repo, "batch_get", AsyncMock(side_effect=lambda keys: {k: table[k] for k in keys if k in table})
    )
    monkeypatch.setattr(repo, "get_room", AsyncMock(side_effect=lambda _room: table[(pk, "META")]))

    class Table:
        def update_item(self, *, Key, ExpressionAttributeValues, **_):
            values = ExpressionAttributeValues
            table[(Key["pk"], Key["sk"])] = {"lastReadSeq": values[":n"], "topOrdinal": values[":o"]}

    monkeypatch.setattr(repo, "_table", lambda: Table())
    assert await session_chat.chat_state(OWNER, BOB.user_id, [SID]) == {SID: (ROOM, 3)}
    assert await repo.set_read_cursor(ROOM, BOB.user_id, 3) == 3  # the chat column's PUT .../read
    assert await session_chat.chat_state(OWNER, BOB.user_id, [SID]) == {SID: (ROOM, 0)}
    assert await session_chat.chat_state(OWNER, BOB.user_id, [SID]) == {SID: (ROOM, 0)}  # and after a reload
    table[(pk, "META")] = {**table[(pk, "META")], "lastSeq": 4, "topCount": 4}  # one reply while the chat is closed
    assert await session_chat.chat_state(OWNER, BOB.user_id, [SID]) == {SID: (ROOM, 1)}


@pytest.mark.asyncio
async def test_a_lifecycle_write_that_loses_the_revision_race_is_tried_again(env):
    """A message or a seat can move the room's revision under the end/resume hook, which runs once."""
    from botocore.exceptions import ClientError

    lost = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    env["get_room"].return_value = META
    env["session"].return_value = {**ROW, "endedAt": 10}
    env["patch_room_meta"].side_effect = [lost, {**META, "archivedAt": "t", "revision": 3}]
    await session_chat.follow_session(OWNER, SID)
    assert env["patch_room_meta"].await_count == 2
    messaging._publish_room_metadata.assert_awaited_once()
    # Another writer already installed the state: nothing left to write.
    env["patch_room_meta"].reset_mock(side_effect=True)
    env["patch_room_meta"].side_effect = [lost]
    env["get_room"].side_effect = [META, {**META, "archivedAt": "t"}]
    await session_chat.follow_session(OWNER, SID)
    assert env["patch_room_meta"].await_count == 1


@pytest.mark.asyncio
async def test_the_chat_is_named_after_the_session_and_follows_a_rename(env, monkeypatch):
    """The agent's turn and the phone's header read the room name: it tracks the session's title."""
    env["get_room"].return_value = {**META, "name": "Session chat · Session"}
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    await session_chat.open_chat(BOB, SID)
    env["patch_room_meta"].assert_awaited_once_with(ROOM, {"name": "Session chat · Fix relay"})
    env["patch_room_meta"].reset_mock()
    env["get_room"].return_value = {**META, "name": "Session chat · Fix relay"}
    await session_chat.open_chat(BOB, SID)
    env["patch_room_meta"].assert_not_awaited()
    # Nobody renames it by hand: the next open would only take the name back.
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, {"role": "owner"})))
    with pytest.raises(ApiProblem) as refused:
        await messaging.patch_room(ALICE, ROOM, messaging.PatchRoomRequest(name="mine"))
    assert refused.value.error.code == "ROOM_FORBIDDEN"


@pytest.mark.asyncio
async def test_a_room_created_as_its_session_ends_is_archived_from_the_fresh_row(env):
    """The end hook found no room yet: the door reads the session again after creating one."""
    env["session"].return_value = {**ROW, "endedAt": 10}  # the door's own read was still live
    await session_chat.open_chat(BOB, SID)
    updates = env["patch_room_meta"].await_args.args[1]
    assert updates["archivedAt"] and updates["archivedBy"] == "__system__"


@pytest.mark.asyncio
async def test_silence_counts_a_member_someone_else_already_removed_as_done(monkeypatch):
    """A pause's silence can race the door's own cleanup of the same seat: gone is the goal, not a 500."""
    from botocore.exceptions import ClientError

    lost = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    members = [{"kind": "human", "id": ALICE.user_id}, {"kind": "human", "id": BOB.user_id}]
    monkeypatch.setattr(session_chat.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(session_chat.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(session_chat.messaging_repo, "get_member", AsyncMock(return_value=None))
    remove = AsyncMock(side_effect=lost)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", remove)
    assert await session_chat.silence(OWNER, SID, keep=ALICE.user_id) == 0
    remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_archiving_the_chat_closes_its_huddle_and_reopening_does_not(env):
    env["get_room"].return_value = META
    env["session"].return_value = {**ROW, "endedAt": 10}
    await session_chat.follow_session(OWNER, SID)
    env["close_huddle"].assert_awaited_once_with(ROOM)
    env["close_huddle"].reset_mock()
    env["get_room"].return_value = {**META, "archivedAt": "t"}
    env["session"].return_value = ROW
    await session_chat.follow_session(OWNER, SID)
    env["close_huddle"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_late_end_handler_applies_what_the_session_row_says_now(env):
    """An end handler delayed past a resume must not archive the resumed session's chat."""
    env["get_room"].return_value = META
    env["session"].return_value = ROW  # resumed since the handler was queued
    await session_chat.follow_session(OWNER, SID)
    env["patch_room_meta"].assert_not_awaited()
    # The row moves under the write (ended, then resumed): the hook follows it to the end.
    env["session"].side_effect = [{**ROW, "endedAt": 10}, ROW, ROW]
    await session_chat.follow_session(OWNER, SID)
    writes = [c.args[1] for c in env["patch_room_meta"].await_args_list]
    assert writes[0]["archivedAt"] and writes[1] == {"archivedAt": None, "archivedBy": None}


@pytest.mark.asyncio
async def test_no_chat_opens_for_a_session_whose_person_left_the_workspace(env):
    env["get_user_stream"].return_value = {"active": False}
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    env["create_room"].assert_not_awaited()
    env["upsert_member"].assert_not_awaited()
    env["ensure_user_stream"].assert_not_awaited()  # a revoked person's stream is never reopened here


@pytest.mark.asyncio
async def test_a_seat_that_lands_after_the_owner_was_revoked_is_taken_back(env, monkeypatch):
    """revoke_human_membership may silence the room before this seat lands (the row is still shared)."""
    env["get_room"].return_value = META
    alice = [{"active": True}, {"active": False}]  # at the door, then removed
    env["get_user_stream"].side_effect = lambda owner, uid: alice.pop(0) if uid == ALICE.user_id else {"active": True}
    silence = AsyncMock(return_value=1)
    monkeypatch.setattr(session_chat, "silence", silence)
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    silence.assert_awaited_once_with(OWNER, SID, keep=None)


@pytest.mark.asyncio
async def test_a_reader_revoked_while_being_seated_loses_only_their_own_seat(env, monkeypatch):
    env["get_room"].return_value = META
    bob = [{"active": True}, {"active": True}, {"active": False}]  # door, seat, then removed
    env["get_user_stream"].side_effect = lambda owner, uid: {"active": True} if uid == ALICE.user_id else bob.pop(0)
    unseat = AsyncMock(return_value=True)
    monkeypatch.setattr(session_chat, "_unseat", unseat)
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, SID)
    unseat.assert_awaited_once_with(ROOM, BOB.user_id)


@pytest.mark.asyncio
async def test_a_removed_owners_chats_are_silenced_after_their_stream_is_off(monkeypatch):
    """The door's check reads the stream: silencing first would leave a gap a late seat slips through."""
    order: list[str] = []
    entry = {"meta": META}
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=[entry]))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value={"id": ALICE.user_id}))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging, "_remove_member_with_notices", AsyncMock())
    monkeypatch.setattr(
        messaging.messaging_repo, "deactivate_user_stream", AsyncMock(side_effect=lambda *a: order.append("stream off"))
    )
    monkeypatch.setattr(session_chat, "silence", AsyncMock(side_effect=lambda *a, **k: order.append("silence")))
    await messaging.revoke_human_membership(OWNER, ALICE.user_id)
    assert order == ["stream off", "silence"]
    session_chat.silence.assert_awaited_once_with(OWNER, SID, keep=None)


@pytest.mark.asyncio
async def test_a_reader_with_no_stream_yet_gets_one_and_a_removed_reader_is_refused_before_any_write(env, monkeypatch):
    monkeypatch.setattr(session_chat, "_unseat", AsyncMock(return_value=True))
    env["get_room"].return_value = META
    env["get_user_stream"].side_effect = lambda owner, uid: {"active": True} if uid == ALICE.user_id else None
    with pytest.raises(ApiProblem):  # seated, then the post-seat check finds no stream in this fake
        await session_chat.open_chat(BOB, SID)
    env["ensure_user_stream"].assert_awaited_once_with(OWNER, BOB.user_id)
    env["ensure_user_stream"].reset_mock()
    env["upsert_member"].reset_mock()
    env["get_user_stream"].side_effect = lambda owner, uid: {"active": uid == ALICE.user_id}
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    env["ensure_user_stream"].assert_not_awaited()
    env["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_removed_as_they_create_the_chat_gets_no_room_key(env, monkeypatch):
    """The first open by the session's own person seats them in the create itself: the door's
    closing check runs on every path, not only after a new seat."""
    env["get_member"].return_value = {"kind": "human", "id": ALICE.user_id}  # seated by the create
    alice = [{"active": True}, {"active": True}, {"active": False}]  # door (as owner, as reader), then removed
    env["get_user_stream"].side_effect = lambda owner, uid: alice.pop(0)
    silence = AsyncMock(return_value=1)
    monkeypatch.setattr(session_chat, "silence", silence)
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(ALICE, SID)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    env["create_room"].assert_awaited_once()
    env["upsert_member"].assert_not_awaited()
    silence.assert_awaited_once_with(OWNER, SID, keep=None)
    messaging._room_detail.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reader_already_seated_is_checked_again_on_every_open(env, monkeypatch):
    env["get_room"].return_value = META
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    session_chat.local_sessions.authorize_session.side_effect = [(ROW, {}), gone]
    silence = AsyncMock(return_value=1)
    monkeypatch.setattr(session_chat, "silence", silence)
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, SID)
    silence.assert_awaited_once_with(OWNER, SID, keep=ALICE.user_id)
