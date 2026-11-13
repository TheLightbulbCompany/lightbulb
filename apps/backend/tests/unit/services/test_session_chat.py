"""The session chat door (PR #3807)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
