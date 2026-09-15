"""Shared (vendor) rooms: foreign members are authorised and notified by their HOME workspace."""

import uuid
from unittest.mock import AsyncMock, call

import pytest
from core.auth import AuthContext
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


@pytest.fixture
def vendor(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", VENDOR)
    return VENDOR


def test_vendor_room_is_recognised_by_its_deterministic_id(vendor):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    assert messaging.is_vendor_room({"roomId": room_id, "ownerId": CUSTOMER})
    assert not messaging.is_vendor_room({"roomId": ROOM, "ownerId": CUSTOMER})
    fields = messaging._room_summary_fields({**META, "roomId": room_id}, _human("user_c", CUSTOMER), 0)
    assert fields["shared"] is True and fields["workspace_id"] == META["workspaceId"]
    assert messaging._room_summary_fields(META, _human("user_c", CUSTOMER), 0)["shared"] is False


def test_feature_off_means_no_room_is_ever_shared(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    assert not messaging.is_vendor_room(
        {"roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER), "ownerId": CUSTOMER}
    )


@pytest.mark.asyncio
async def test_vendor_room_membership_and_name_are_platform_managed(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    monkeypatch.setattr(
        messaging, "require_room_admin", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner")))
    )
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER))))
    attempts = [
        messaging.upsert_member(
            auth,
            meta["roomId"],
            messaging.UpsertRoomMemberRequest(kind="human", id="user_x", role="member", listen_mode=None),
        ),
        messaging.remove_member(auth, meta["roomId"], "human", "user_v"),
        messaging.leave_room(auth, meta["roomId"]),
        messaging.patch_room(auth, meta["roomId"], messaging.PatchRoomRequest(name="renamed")),
    ]
    for attempt in attempts:
        with pytest.raises(ApiProblem) as exc:
            await attempt
        assert exc.value.error.code == "ROOM_FORBIDDEN"


@pytest.mark.asyncio
async def test_vendor_room_still_lets_admins_change_the_agents_listen_mode(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    agent = {
        "kind": "agent",
        "id": str(uuid.uuid4()),
        "role": "member",
        "listenMode": "mentions",
        "displayName": "CEO",
        "mentionHandle": "ceo",
    }
    monkeypatch.setattr(
        messaging, "require_room_admin", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner")))
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=agent))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": agent["id"], "name": "CEO"}]))
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value={**agent, "listenMode": "active"}))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    result = await messaging.upsert_member(
        auth,
        meta["roomId"],
        messaging.UpsertRoomMemberRequest(kind="agent", id=agent["id"], role="member", listen_mode="active"),
    )
    assert result == "detail"
