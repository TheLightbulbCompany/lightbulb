"""The session chat door (spec 2026-10-07-session-chat-design §2, §4.1)."""

import inspect
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
        "patch_room_meta": AsyncMock(side_effect=lambda room_id, updates: {**META, **updates, "revision": 2}),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(session_chat.messaging_repo, name, fake)
    monkeypatch.setattr(session_chat.local_sessions, "authorize_session", AsyncMock(return_value=(ROW, {})))
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[CEO]))
    monkeypatch.setattr(
        messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid.removeprefix("user_").title())
    )
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(side_effect=lambda meta, user_id, member=None: meta))
    metrics: list[str] = []
    monkeypatch.setattr(session_chat, "put_metric", lambda name, *a, **k: metrics.append(name))
    fakes["metrics"] = metrics
    return fakes


@pytest.mark.asyncio
async def test_first_open_creates_one_hidden_room_and_seats_the_reader(env):
    await session_chat.open_chat(BOB, SID)
    session_chat.local_sessions.authorize_session.assert_awaited_once_with(BOB, None, SID)
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
    env["ensure_user_stream"].assert_any_await(OWNER, BOB.user_id)


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
    session_chat.local_sessions.authorize_session.return_value = ({**ROW, "endedAt": 10}, {})
    await session_chat.open_chat(BOB, SID)
    updates = env["patch_room_meta"].await_args.args[1]
    assert updates["archivedAt"] and updates["archivedBy"] == "__system__"
    # The same conversation resumed under the same id: the chat takes posts again.
    env["get_room"].return_value = {**META, "archivedAt": "2026-10-07T00:00:00+00:00"}
    session_chat.local_sessions.authorize_session.return_value = (ROW, {})
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
