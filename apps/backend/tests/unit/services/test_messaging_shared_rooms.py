"""Shared (vendor) rooms: foreign members are authorised and notified by their HOME workspace."""

import uuid
from unittest.mock import AsyncMock, call

import pytest
from core.services import messaging
from core.services.messaging import ApiProblem

ROOM = str(uuid.uuid4())
CUSTOMER, VENDOR = "org_customer", "org_vendor"
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
async def test_revoking_an_org_membership_only_touches_rooms_where_that_org_is_home(monkeypatch):
    foreign = _human("user_v", VENDOR)
    discovered = [{"meta": META, "member": foreign}]
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=discovered))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=foreign))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "deactivate_user_stream", AsyncMock())
    removed = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", removed)

    await messaging.revoke_human_membership(VENDOR, "user_v")
    removed.assert_awaited_once_with(META, "human", "user_v")

    removed.reset_mock()
    await messaging.revoke_human_membership(CUSTOMER, "user_v")
    removed.assert_not_awaited()
