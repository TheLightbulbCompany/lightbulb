"""Rooms with a foreign member: they are authorised and notified by their HOME tenant."""

import uuid
from unittest.mock import AsyncMock, call

import pytest
from core.auth import AuthContext
from core.services import messaging
from core.services.messaging import ApiProblem

ROOM = str(uuid.uuid4())
CUSTOMER, VENDOR = "user_customer", "user_foreign"
META = {
    "roomId": ROOM,
    "ownerId": CUSTOMER,
    "kind": "channel",
    "revision": 2,
    "lastSeq": 7,
    "deliveryKey": str(uuid.uuid4()),
    "workspaceId": str(uuid.uuid4()),
    "updatedAt": "2026-09-14T00:00:00Z",
    "lastMessageAt": None,
    "createdAt": "2026-09-14T00:00:00Z",
}


def _human(user_id: str, home: str, role: str = "member") -> dict:
    return {
        "kind": "human",
        "id": user_id,
        "ownerId": CUSTOMER,
        "homeOwnerId": home,
        "role": role,
        "joinedSeq": 0,
        "displayName": user_id,
        "mentionHandle": None,
        "listenMode": None,
        "joinedAt": "2026-09-14T00:00:00Z",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member, owner_id, expected",
    [
        (_human("user_v", VENDOR), VENDOR, None),
        (_human("user_v", VENDOR), CUSTOMER, "ROOM_NOT_FOUND"),
        (None, CUSTOMER, "ROOM_FORBIDDEN"),
        (None, VENDOR, "ROOM_NOT_FOUND"),
    ],
)
async def test_require_room_member_follows_the_members_home(monkeypatch, member, owner_id, expected):
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=member))
    if expected is None:
        assert await messaging.require_room_member(owner_id, "user_v", ROOM) == (META, member)
        return
    with pytest.raises(ApiProblem) as exc:
        await messaging.require_room_member(owner_id, "user_v", ROOM)
    assert exc.value.error.code == expected
    assert (exc.value.error.reconcile_from_seq == 7) is (expected == "ROOM_FORBIDDEN")


@pytest.mark.asyncio
async def test_a_member_row_left_behind_by_a_meta_only_migration_still_opens_its_room(monkeypatch):
    # The #2627 personal->org migration rewrote META.ownerId but never the MEMBER# rows.
    legacy = {
        "kind": "human",
        "id": "user_x",
        "ownerId": "user_x",
        "memberKey": "OWNER#user_x#human#user_x",
        "role": "owner",
        "joinedSeq": 0,
    }
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=legacy))
    assert await messaging.require_room_member(CUSTOMER, "user_x", ROOM) == (META, legacy)


@pytest.mark.asyncio
async def test_removal_notices_go_to_each_humans_home_stream(monkeypatch):
    members = [_human("user_c", CUSTOMER, "owner"), _human("user_v", VENDOR)]
    streams = AsyncMock(side_effect=lambda owner, user: f"key-{owner}-{user}")
    remove = AsyncMock(return_value={**META, "revision": 3})
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", remove)

    await messaging._remove_member_with_notices(META, "human", "user_v")

    assert streams.await_args_list == [call(CUSTOMER, "user_c"), call(VENDOR, "user_v")]
    notices = remove.await_args.kwargs["live_events"]
    assert [(n["userId"], n["ownerId"], n["channelKey"]) for n in notices] == [
        ("user_c", CUSTOMER, f"key-{CUSTOMER}-user_c"),
        ("user_v", VENDOR, f"key-{VENDOR}-user_v"),
    ]
    assert [n["event"]["type"] for n in notices] == ["room.subscription.rotated", "room.access.removed"]


@pytest.mark.asyncio
async def test_attachment_download_reads_the_rooms_owner_workspace_not_the_callers(monkeypatch):
    from types import SimpleNamespace

    from routers import messaging as router

    seen: list[str] = []
    monkeypatch.setattr(router.service, "require_room_member", AsyncMock(return_value=(META, _human("user_v", VENDOR))))
    monkeypatch.setattr(
        router.service,
        "load_attachment",
        lambda owner, room, attachment_id: (
            seen.append(owner),
            (SimpleNamespace(content_type="image/png", file_name="a.png"), b"x"),
        )[1],
    )
    response = await router.get_room_attachment(
        ROOM, str(uuid.uuid4()), AuthContext(user_id=VENDOR, human_session=True)
    )
    assert response.status_code == 200 and seen == [CUSTOMER]


@pytest.mark.asyncio
async def test_a_foreign_members_message_is_written_under_the_rooms_owner(monkeypatch):
    sender = _human("user_v", VENDOR)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, sender)))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[sender]))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    stored = {
        **META,
        "messageId": str(uuid.uuid4()),
        "messageSeq": 8,
        "roomRevision": 2,
        "author": {"kind": "human", "id": "user_v", "displayName": "Vee"},
        "text": "hi",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": "2026-09-14T00:00:00Z",
        "runId": None,
        "dispatchTargets": [],
    }
    create = AsyncMock(return_value=(stored, False))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    touched, captured = [], []
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", lambda owner: touched.append(owner) or _noop())
    monkeypatch.setattr(
        "core.services.onboarding_brief.capture_first_workspace_message",
        lambda owner, item: captured.append(owner) or _noop(),
    )
    await messaging.create_human_message(
        AuthContext(user_id=VENDOR, human_session=True),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(),
            text="hi",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )
    assert create.await_args.kwargs["owner_id"] == CUSTOMER
    assert touched == [CUSTOMER] and captured == [CUSTOMER]


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_a_foreign_admin_can_only_add_the_rooms_own_org_members(monkeypatch):
    admin = _human("user_v", VENDOR, "admin")
    agent_id = str(uuid.uuid4())
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, admin)))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    load_agent_roster = AsyncMock(return_value=[{"id": agent_id, "name": "CEO"}])
    monkeypatch.setattr(messaging, "load_agent_roster", load_agent_roster)
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value={"kind": "agent", "id": agent_id}))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    upsert_member = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert_member)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))

    await messaging.upsert_member(
        AuthContext(user_id=VENDOR, human_session=True),
        ROOM,
        messaging.UpsertRoomMemberRequest(kind="agent", id=agent_id, role="member", listen_mode="mentions"),
    )
    assert load_agent_roster.await_args.args == (CUSTOMER,)
    assert upsert_member.await_args.kwargs["owner_id"] == CUSTOMER


CEO = str(uuid.uuid4())


def test_summary_marks_a_foreign_member_as_guest():
    assert messaging._room_summary_fields(META, _human("user_v", VENDOR), 0)["guest"] is True
    assert messaging._room_summary_fields(META, _human("user_c", CUSTOMER), 0)["guest"] is False
