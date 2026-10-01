"""Owner-only channel delete: a tombstone that rotates delivery like a human removal."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from core.services.messaging import ApiProblem
from core.services.openclaw_work import OpenClawWorkWorker
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    _member,
    _post,
    _room,
    repo,
    table,
)

USER_C = "user_C"


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


def _auth(user):
    return AuthContext(user_id=user, org_id=OWNER)


async def _channel():
    return await _room(
        [
            _member("human", USER_A, "owner"),
            _member("human", USER_B, "admin"),
            _member("human", USER_C, "member"),
            _member("agent", FRIDAY, handle="friday"),
        ]
    )


async def _problem(coro):
    with pytest.raises(ApiProblem) as exc:
        await coro
    return exc.value.error.code


@pytest.mark.asyncio
async def test_owner_deletes_a_channel_as_a_tombstone_that_rotates_delivery(table):
    room_id, meta = await _channel()
    await _post(room_id, text="history")

    await messaging.delete_room(_auth(USER_A), room_id)

    after = await repo.get_room(room_id)
    assert after["deletedAt"] and after["deletedBy"] == USER_A
    assert after["deliveryKey"] != meta["deliveryKey"]
    assert after["revision"] == meta["revision"] + 1
    # Soft delete: messages and members stay.
    assert (await repo.get_message(room_id, 1))["text"] == "history"
    assert {m["id"] for m in await repo.list_members(room_id)} == {USER_A, USER_B, USER_C, FRIDAY}
    # Every human got the same notice clients already drop a room on; none carries the new key.
    live = [i for i in table.scan()["Items"] if i["pk"] == f"ROOM#{room_id}" and i["sk"].startswith("LIVE#")]
    assert {i["userId"] for i in live} == {USER_A, USER_B, USER_C}
    assert {i["event"]["type"] for i in live} == {"room.access.removed"}
    assert all("deliveryKey" not in i["event"]["payload"] for i in live)


@pytest.mark.asyncio
async def test_after_delete_every_door_is_a_404_and_the_list_drops_it(table):
    room_id, _ = await _channel()
    await messaging.delete_room(_auth(USER_A), room_id)

    for user in (USER_A, USER_B, USER_C):
        rooms = await messaging.list_rooms(_auth(user))
        assert room_id not in {str(r.room_id) for r in rooms.rooms}
        assert await _problem(messaging.get_room(_auth(user), room_id)) == "ROOM_NOT_FOUND"
        assert await _problem(messaging.list_messages(_auth(user), room_id, 0, 50)) == "ROOM_NOT_FOUND"
    # The repo fence refuses a post that got past every door.
    with pytest.raises(repo.RoomNotFound):
        await _post(room_id, client_id="late")
    # Idempotency: a second delete is a 404.
    assert await _problem(messaging.delete_room(_auth(USER_A), room_id)) == "ROOM_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_delete_landing_between_the_meta_read_and_the_commit_still_refuses_the_post(table, monkeypatch):
    room_id, _ = await _channel()
    stale = await repo.get_room(room_id)
    await messaging.delete_room(_auth(USER_A), room_id)
    real_get_room = repo.get_room
    reads = iter([stale])

    async def get_room(rid):
        return next(reads, None) or await real_get_room(rid)

    monkeypatch.setattr(repo, "get_room", get_room)
    with pytest.raises(repo.RoomNotFound):
        await _post(room_id, client_id="racing")
    assert (await real_get_room(room_id))["lastSeq"] == 0


@pytest.mark.parametrize("user", [USER_B, USER_C])
@pytest.mark.asyncio
async def test_only_the_owner_may_delete(table, user):
    room_id, _ = await _channel()
    assert await _problem(messaging.delete_room(_auth(user), room_id)) == "ROOM_FORBIDDEN"
    assert "deletedAt" not in await repo.get_room(room_id)


@pytest.mark.parametrize("kind", ["dm", "group-dm"])
@pytest.mark.asyncio
async def test_dms_are_not_deletable(monkeypatch, kind):
    meta = {"roomId": "r", "ownerId": OWNER, "kind": kind, "revision": 1}
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, {"role": "owner"})))
    delete = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "delete_room", delete)
    assert await _problem(messaging.delete_room(_auth(USER_A), "r")) == "ROOM_NOT_DELETABLE"
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_general_and_the_shared_vendor_channel_are_not_deletable(monkeypatch):
    delete = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "delete_room", delete)
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "org_vendor")
    for room_id in (messaging.default_room_id(OWNER), messaging.messaging_repo.vendor_room_id(OWNER)):
        meta = {"roomId": room_id, "ownerId": OWNER, "kind": "channel", "revision": 1}
        monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, {"role": "owner"})))
        assert await _problem(messaging.delete_room(_auth(USER_A), room_id)) == "ROOM_NOT_DELETABLE"
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_replies_and_follow_ups_into_a_deleted_room_are_404(monkeypatch):
    meta = {"roomId": "r", "ownerId": OWNER, "kind": "channel", "deletedAt": "2026-09-23T00:00:00Z"}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    request = messaging.FollowUpRequest.model_validate(
        {
            "roomId": "00000000-0000-4000-8000-000000000001",
            "agentId": FRIDAY,
            "followUpId": "00000000-0000-4000-8000-000000000002",
            "triggerMessageId": "00000000-0000-4000-8000-000000000003",
            "eventKind": "user_request",
            "reason": "delegation_completed",
            "text": "done",
        }
    )
    assert await _problem(messaging.create_follow_up(OWNER, request)) == "ROOM_NOT_FOUND"
    reply = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "00000000-0000-4000-8000-000000000004",
            "roomId": "00000000-0000-4000-8000-000000000001",
            "agentId": FRIDAY,
            "text": "late reply",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    assert await _problem(messaging.create_agent_message(OWNER, reply)) == "ROOM_NOT_FOUND"


@pytest.mark.asyncio
async def test_dispatch_queued_before_the_delete_wakes_nobody_but_advances_the_cursor(table, monkeypatch):
    room_id, _ = await _channel()
    await _post(room_id, targets=[{"agentId": FRIDAY, "eventKind": "user_request"}])
    await messaging.delete_room(_auth(USER_A), room_id)
    worker = OpenClawWorkWorker(sqs_client=MagicMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch_message", dispatch)
    await worker._room_dispatch({"roomId": room_id, "agentId": FRIDAY, "throughSeq": 1})
    dispatch.assert_not_awaited()
    assert int((await repo.get_agent_delivery(room_id, FRIDAY))["lastSeq"]) == 1


@pytest.mark.asyncio
async def test_removal_and_delete_revoke_the_rooms_huddle(table, monkeypatch):
    """Member removal is a security boundary: the removed member leaves the LiveKit room too,
    and a deleted channel's huddle ends for everyone."""
    removed, closed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(messaging.livekit_huddles, "remove_participant", removed)
    monkeypatch.setattr(messaging.livekit_huddles, "close_room", closed)
    room_id, _ = await _channel()

    await messaging.remove_member(_auth(USER_A), room_id, "human", USER_C)
    removed.assert_awaited_once_with(room_id, USER_C)
    await messaging.remove_member(_auth(USER_A), room_id, "agent", FRIDAY)
    removed.assert_awaited_once()  # agents are never in the LiveKit room

    await messaging.delete_room(_auth(USER_A), room_id)
    closed.assert_awaited_once_with(room_id)
