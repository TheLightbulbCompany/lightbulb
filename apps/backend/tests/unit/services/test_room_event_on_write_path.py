"""The room's ``room.message.created`` event is published from the write path, and it is
byte-for-byte the event the projector publishes later for the same row, so the client's
eventId dedupe keeps one copy (packages/shared/src/live/appsyncEvents.ts ``seen``)."""

import asyncio
import json
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.services import live_events, messaging
from tests.unit.services.test_messaging_addressed_agent_ids import FRIDAY, _agent_member, _human_member, _send, _wire

_INDEX = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "messaging-projector" / "index.py"
ROOM = str(uuid.UUID(int=9))
KEY = str(uuid.UUID(int=7))
WS = str(uuid.UUID(int=5))
CHANNEL = f"/ws/{WS}/rooms/{ROOM}/{KEY}"


def _projector():
    spec = importlib.util.spec_from_file_location("messaging_projector_index_write_path", _INDEX)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _projector_bytes(item: dict) -> tuple[str, str]:
    """What the projector's ``_fan_out_message`` publishes for ``item``, its I/O stubbed."""
    projector = _projector()
    sent = []
    projector._get_item = lambda pk, sk: item if sk == item["sk"] else {"deliveryKey": KEY, "ownerId": "org_1"}
    projector._workspace_id_for_meta = lambda *_: WS
    projector._notify_members = lambda *_: []
    projector._enqueue_search_index_on_insert = lambda *_: None
    projector._send_push = lambda *_: None
    projector._emit_metric = lambda *_: None
    projector._publish = lambda channel, event: sent.append((channel, projector._event_json(event)))
    projector._fan_out_message(item)()
    assert len(sent) == 1
    return sent[0]


async def _backend_bytes(item: dict) -> tuple[str, str]:
    publisher = live_events.LiveEventPublisher()
    publisher._room_channel = AsyncMock(return_value=CHANNEL)
    publisher._send = AsyncMock()
    rows = {(f"ROOM#{ROOM}", "META"): {"deliveryKey": KEY}, (item["pk"], item["sk"]): item}
    with patch.object(live_events.messaging_repo, "batch_get", AsyncMock(return_value=rows)):
        assert await publisher.publish_message_created(item) is True
    channel, body = publisher._send.await_args.args
    assert publisher._send.await_args.kwargs == {"source": "write_path", "held": True}
    return channel, body


def _item(**extra) -> dict:
    message_id = str(uuid.uuid4())
    return {
        "pk": f"ROOM#{ROOM}",
        "sk": f"MSG#{3:020d}",
        "messageId": message_id,
        "roomId": ROOM,
        "ownerId": "org_1",
        "messageSeq": 3,
        "roomRevision": 2,
        "author": {"kind": "human", "id": "user_owner", "displayName": "Owner", "extra": "dropped"},
        "text": "héllo ✨ @friday",
        "mentions": [{"kind": "agent", "id": FRIDAY, "handle": "dropped"}],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": "2026-10-08T12:34:56.123456+00:00",
        "runId": None,
        "dispatchTargets": [
            {"agentId": FRIDAY, "eventKind": "user_request"},
            {"agentId": "other", "eventKind": "room_event"},
        ],
        "sourceAgentId": None,
        "clientMessageId": "c-1",
        "requestHash": "h",
        "attachments": [],
        "topOrdinal": 1,
        **extra,
    }


SESSION = str(uuid.UUID(int=3))


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"attachments": [{"id": "a1", "fileName": "x.png", "contentType": "image/png", "size": 12, "url": "drop"}]},
        {"sessionMentions": [SESSION], "sessionTargetLabels": {SESSION: "Codex · fix · Ann", "stale": "drop"}},
        {"progress": True, "silent": True, "runId": "run-1", "via": "mcp", "actorUserId": "u"},
        {"threadRootMessageId": str(uuid.UUID(int=11)), "replyToMessageId": str(uuid.UUID(int=11))},
        # Super-agent attrs ride the live copy, or a Router reply's task cards appear only after a reload.
        {"taskRefs": [{"taskId": "t-1", "role": "started"}], "specialist": "finance", "kind": "result"},
        {"kind": "automation_confirm", "automationId": "01JAUTO"},  # the card links to its automation
    ],
)
async def test_write_path_event_is_byte_equal_to_the_projectors(extra):
    item = _item(**extra)
    assert await _backend_bytes(item) == _projector_bytes(item)


async def test_confirm_card_event_carries_its_automation_id():
    item = _item(kind="automation_confirm", automationId="01JAUTO")
    backend, projector = await _backend_bytes(item), _projector_bytes(item)
    assert '"automationId":"01JAUTO"' in backend[1] and backend == projector


@pytest.fixture(autouse=True)
def _own_tasks(monkeypatch):
    # Another test's loop may close before its task's done-callback runs; start from an empty set.
    monkeypatch.setattr(messaging, "_write_path_tasks", set())


async def _settle():
    await asyncio.gather(*list(messaging._write_path_tasks))


def _with_keys(monkeypatch) -> list[dict]:
    """The stored row carries its keys, as messaging_repo.create_message's does. Returns the rows."""
    fake = messaging.messaging_repo.create_message.side_effect
    rows: list[dict] = []

    async def create_message(**kwargs):
        item, replayed = await fake(**kwargs)
        rows.append({"pk": f"ROOM#{item['roomId']}", "sk": f"MSG#{item['messageSeq']:020d}", **item})
        return rows[-1], replayed

    monkeypatch.setattr(messaging.messaging_repo, "create_message", AsyncMock(side_effect=create_message))
    return rows


async def test_create_human_message_publishes_the_projectors_event_once(monkeypatch):
    _wire(monkeypatch, [_human_member(), _agent_member()])
    rows = _with_keys(monkeypatch)
    publisher = live_events.LiveEventPublisher()
    publisher._room_channel = AsyncMock(return_value=CHANNEL)
    publisher._send = AsyncMock()
    monkeypatch.setattr(
        live_events.messaging_repo,
        "batch_get",
        AsyncMock(
            side_effect=lambda keys: {
                (f"ROOM#{ROOM}", "META"): {"deliveryKey": KEY},
                **{(r["pk"], r["sk"]): r for r in rows},
            }
        ),
    )
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: publisher)
    await _send("hi @friday", mentions=[{"kind": "agent", "id": FRIDAY}])
    await _settle()
    assert publisher._send.await_count == 1
    assert publisher._send.await_args.args == _projector_bytes(rows[0])


async def test_a_failing_publish_does_not_fail_the_write(monkeypatch):
    _wire(monkeypatch, [_human_member(), _agent_member()])
    _with_keys(monkeypatch)
    failing = AsyncMock(side_effect=live_events.LivePublishError("AppSync down"))
    monkeypatch.setattr(
        messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_message_created": failing})()
    )
    message = await _send("still stored")
    await _settle()
    assert message.text == "still stored"
    failing.assert_awaited_once()


def _waiting_publish(monkeypatch, rows):
    """A write-path publish that will park on the only slot; ``rows()`` is what its one read answers."""
    monkeypatch.setattr(live_events.messaging_repo, "batch_get", AsyncMock(side_effect=lambda _k: rows()))
    monkeypatch.setattr(live_events.messaging_repo, "workspace_id_for_meta", AsyncMock(return_value=WS))
    monkeypatch.setattr(live_events, "_inflight", asyncio.Semaphore(1))
    monkeypatch.setattr(live_events.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.test/event")
    posted = []

    async def post(_endpoint, body, *_a):
        posted.append(body)
        return type("R", (), {"status_code": 200, "json": lambda self: {"successful": [{}], "failed": []}})()

    async def sign(*_a):
        return type("Q", (), {"headers": {}})()

    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_post_held", post)
    monkeypatch.setattr(publisher, "_signed_request", sign)
    return publisher, posted


@pytest.mark.asyncio
async def test_a_key_rotated_while_waiting_for_a_slot_is_the_channel_posted_to(monkeypatch):
    """Codex on #3930: the channel is read after the slot wait, so a removal that rotates the
    delivery key meanwhile does not send the message to the channel the removed member holds."""
    item = _item()
    meta = {"deliveryKey": KEY}
    publisher, posted = _waiting_publish(
        monkeypatch, lambda: {(f"ROOM#{ROOM}", "META"): dict(meta), (item["pk"], item["sk"]): item}
    )
    await live_events._inflight.acquire()  # a slow publish holds the only slot
    task = asyncio.create_task(publisher.publish_message_created(item))
    await asyncio.sleep(0.01)
    new_key = str(uuid.UUID(int=8))
    meta["deliveryKey"] = new_key  # a human is removed: the room's key rotates
    live_events._inflight.release()
    assert await task is True
    assert f"/rooms/{ROOM}/{new_key}".encode() in posted[0]
    assert KEY.encode() not in posted[0]


@pytest.mark.asyncio
async def test_a_message_deleted_while_waiting_for_a_slot_is_not_broadcast(monkeypatch):
    """Codex on #3930: the projector re-reads the row for a delete; so does the write path."""
    item = _item()
    stored = {"row": item}
    publisher, posted = _waiting_publish(
        monkeypatch, lambda: {(f"ROOM#{ROOM}", "META"): {"deliveryKey": KEY}, (item["pk"], item["sk"]): stored["row"]}
    )
    await live_events._inflight.acquire()
    task = asyncio.create_task(publisher.publish_message_created(item))
    await asyncio.sleep(0.01)
    stored["row"] = {**item, "messageRevision": 1, "text": ""}  # deleted: the projector sends it
    live_events._inflight.release()
    assert await task is False
    assert posted == []


@pytest.mark.asyncio
async def test_a_failed_resolution_records_one_tagged_error_sample(monkeypatch):
    publisher, posted = _waiting_publish(monkeypatch, dict)
    monkeypatch.setattr(live_events.messaging_repo, "batch_get", AsyncMock(side_effect=TimeoutError("ddb")))
    metrics = []
    monkeypatch.setattr(live_events, "put_metric", lambda *a, **k: metrics.append(k.get("dimensions")))
    with pytest.raises(TimeoutError):
        await publisher.publish_message_created(_item())
    assert metrics == [{"outcome": "error", "status_class": "resolve", "source": "write_path"}]
    assert posted == []


async def test_the_live_copy_carries_a_router_replys_task_cards():
    item = _item(
        taskRefs=[{"taskId": "t-1", "role": "started"}, {"taskId": "t-2", "role": "started"}], specialist="finance"
    )
    _, body = await _backend_bytes(item)
    payload = json.loads(body)["payload"]
    assert payload["taskRefs"] == [{"taskId": "t-1", "role": "started"}, {"taskId": "t-2", "role": "started"}]
    assert payload["specialist"] == "finance"
    assert "routedTo" not in payload  # absent stays absent
