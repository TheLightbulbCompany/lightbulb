from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import messaging, openclaw_work
from core.services.messaging import ReadCursorRequest

ROOM = "22222222-2222-4222-8222-222222222222"
# Captured before the conftest autouse fixture patches the attribute for every test.
_REAL_PUBLISH_ROOM_EVENT = messaging._publish_room_event


def _auth(user_id="user_a"):
    auth = MagicMock()
    auth.org_id = "org_1"
    auth.user_id = user_id
    return auth


@pytest.mark.asyncio
async def test_detail_carries_every_members_read_cursor_and_agent_delivery(monkeypatch):
    from core.repositories import user_repo

    meta = {
        "roomId": ROOM,
        "ownerId": "org_1",
        "kind": "channel",
        "name": "general",
        "revision": 1,
        "lastSeq": 7,
        "updatedAt": "2026-09-26T00:00:00Z",
        "deliveryKey": "88888888-8888-4888-8888-888888888888",
        "createdAt": "2026-09-26T00:00:00Z",
    }

    def member(kind, id_, name):
        return {
            "kind": kind,
            "id": id_,
            "role": "member",
            "displayName": name,
            "mentionHandle": None,
            "listenMode": "active" if kind == "agent" else None,
            "joinedSeq": 0,
            "joinedAt": "2026-09-26T00:00:00Z",
        }

    members = [member("human", "user_a", "Ann"), member("human", "user_b", "Bo"), member("agent", "ceo", "CEO")]
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "get_read_row", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging.messaging_repo, "mention_count", AsyncMock(return_value=0))
    monkeypatch.setattr(messaging.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(user_repo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_read_cursors",
        AsyncMock(
            return_value=[
                {"userId": "user_a", "lastReadSeq": 7},
                {"userId": "user_b", "lastReadSeq": 5},
                {"userId": "user_gone", "lastReadSeq": 9},
            ]
        ),
    )
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_agent_deliveries",
        AsyncMock(return_value=[{"agentId": "ceo", "lastSeq": 6}, {"agentId": "removed", "lastSeq": 7}]),
    )
    body = (await messaging._room_detail(meta, "user_a", members[0])).model_dump(mode="json", by_alias=True)
    assert body["readCursors"] == [{"userId": "user_a", "lastReadSeq": 7}, {"userId": "user_b", "lastReadSeq": 5}]
    assert body["agentDeliveries"] == [{"agentId": "ceo", "lastDeliveredSeq": 6}]


@pytest.mark.asyncio
async def test_moving_a_read_cursor_tells_the_room(monkeypatch):
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": ROOM}, {})))
    monkeypatch.setattr(messaging, "resolve_owner_id", lambda auth: "org_1")
    monkeypatch.setattr(messaging.messaging_repo, "set_read_cursor", AsyncMock(return_value=8))
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    await messaging.set_read_cursor(_auth(), ROOM, ReadCursorRequest(last_read_seq=8))
    event = publish.await_args.args[1]
    assert event["type"] == "room.read.changed"
    assert event["roomId"] == ROOM
    assert event["payload"] == {"roomId": ROOM, "reader": {"kind": "human", "id": "user_a"}, "lastReadSeq": 8}


@pytest.mark.asyncio
async def test_marking_unread_tells_the_room_the_lowered_cursor(monkeypatch):
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": ROOM}, {"joinedSeq": 0})))
    monkeypatch.setattr(messaging, "resolve_owner_id", lambda auth: "org_1")
    monkeypatch.setattr(messaging.messaging_repo, "mark_unread", AsyncMock(return_value=3))
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    await messaging.mark_room_unread(_auth(), ROOM)
    assert publish.await_args.args[1]["payload"] == {
        "roomId": ROOM,
        "reader": {"kind": "human", "id": "user_a"},
        "lastReadSeq": 3,
    }


@pytest.mark.asyncio
async def test_a_started_run_marks_its_trigger_read_by_the_agent(monkeypatch):
    row = {"runId": "r", "roomId": ROOM, "agentId": "ceo", "triggerMessageId": "m1"}
    monkeypatch.setattr(messaging, "announce_run", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=row)):
        await messaging.handle_run_event(
            "o",
            messaging.RunEventRequest(
                run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                at="2026-09-26T00:00:00Z",
                type="run.started",
                session_key="k",
            ),
        )
    event = publish.await_args.args[1]
    assert event["type"] == "room.read.changed"
    assert event["payload"] == {"roomId": ROOM, "reader": {"kind": "agent", "id": "ceo"}, "messageId": "m1"}


@pytest.mark.asyncio
async def test_a_replayed_run_start_publishes_nothing(monkeypatch):
    monkeypatch.setattr(messaging, "announce_run", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    # A replay (start_run returns None because the row was already started) still finds
    # its run, so the idempotency check in handle_run_event doesn't raise RUN_NOT_FOUND.
    monkeypatch.setattr(messaging.messaging_repo, "get_run", AsyncMock(return_value={"runId": "r"}))
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=None)):
        await messaging.handle_run_event(
            "o",
            messaging.RunEventRequest(
                run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                at="2026-09-26T00:00:00Z",
                type="run.started",
                session_key="k",
            ),
        )
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_receipt_publish_never_raises(monkeypatch):
    from core.services import live_events

    publisher = MagicMock()
    publisher.publish_room = AsyncMock(side_effect=RuntimeError("appsync down"))
    monkeypatch.setattr(live_events, "get_live_event_publisher", lambda: publisher)
    # Swap back the real helper the conftest autouse fixture stubs out.
    monkeypatch.setattr(messaging, "_publish_room_event", _REAL_PUBLISH_ROOM_EVENT)
    await messaging._publish_room_event(ROOM, {"type": "room.read.changed"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "advanced, targeted, archived, published",
    [
        (True, True, False, True),
        (False, True, False, False),
        (True, False, False, False),
        # Queued before an archive: nothing was dispatched, so nothing was delivered.
        (True, True, True, False),
    ],
)
async def test_a_delivery_advance_tells_the_room(monkeypatch, advanced, targeted, archived, published):
    agent = "agent-1"
    meta = {"ownerId": "org_1", "roomId": ROOM, "kind": "dm", "lastSeq": 4}
    if archived:
        meta["archivedAt"] = "2026-09-26T00:00:00Z"
    message = {
        "messageId": "m4",
        "messageSeq": 4,
        "author": {"kind": "human", "id": "u1"},
        "dispatchTargets": [{"agentId": agent, "eventKind": "user_request"}] if targeted else [],
    }
    monkeypatch.setattr(
        openclaw_work.messaging_repo, "get_agent_delivery", AsyncMock(side_effect=[{"lastSeq": 3}, {"lastSeq": 4}])
    )
    monkeypatch.setattr(openclaw_work.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(openclaw_work.messaging_repo, "list_messages_between", AsyncMock(return_value=[message]))
    monkeypatch.setattr(
        openclaw_work.messaging_repo, "get_member", AsyncMock(return_value={"kind": "agent", "id": agent})
    )
    monkeypatch.setattr(openclaw_work.messaging_repo, "advance_agent_delivery", AsyncMock(return_value=advanced))
    publish = AsyncMock()
    monkeypatch.setattr(openclaw_work, "publish_delivery_changed", publish)
    worker = openclaw_work.OpenClawWorkWorker(sqs_client=MagicMock())
    monkeypatch.setattr(worker, "_dispatch_message", AsyncMock())
    await worker._room_dispatch({"roomId": ROOM, "agentId": agent, "throughSeq": 4})
    if published:
        publish.assert_awaited_once_with(ROOM, agent, 4)
    else:
        publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_changed_goes_to_the_room_channel(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    await openclaw_work.publish_delivery_changed(ROOM, "agent-1", 4)
    room_id, event = publish.await_args.args
    assert room_id == ROOM
    assert event["type"] == "room.delivery.changed"
    assert event["roomId"] == ROOM
    assert event["payload"] == {"roomId": ROOM, "agentId": "agent-1", "lastDeliveredSeq": 4}
