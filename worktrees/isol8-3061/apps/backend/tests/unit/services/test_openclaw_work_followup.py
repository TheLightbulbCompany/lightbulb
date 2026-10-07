"""Delegation follow-ups on the work queue: pointer shape, handler drops, and the ingress it sends."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories import messaging_repo
from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker, deserialize_pointer

ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
FOLLOW = "33333333-3333-4333-8333-333333333333"
TRIGGER = "44444444-4444-4444-8444-444444444444"
WS = "55555555-5555-4555-8555-555555555555"
KEY = "66666666-6666-4666-8666-666666666666"
META = {"ownerId": "org_1", "roomId": ROOM, "kind": "channel", "revision": 1, "workspaceId": WS, "deliveryKey": KEY}
MEMBER = {"kind": "agent", "id": AGENT}
MESSAGE = {"messageId": TRIGGER, "messageSeq": 4, "author": {"kind": "human", "id": "u1"}}
ROW = {
    "agentId": AGENT,
    "triggerMessageId": TRIGGER,
    "eventKind": "user_request",
    "reason": "delegation_completed",
    "text": "review finished",
}


def _record(payload: dict) -> str:
    return json.dumps({"eventName": "INSERT", "dynamodb": {"NewImage": {"payload": {"M": payload}}}})


def _pointer(**over) -> dict:
    fields = {"kind": "room_follow_up", "roomId": ROOM, "agentId": AGENT, "followUpId": FOLLOW, **over}
    return {key: {"S": value} for key, value in fields.items()}


def test_follow_up_pointer_is_accepted():
    assert deserialize_pointer(_record(_pointer())) == {
        "kind": "room_follow_up",
        "roomId": ROOM,
        "agentId": AGENT,
        "followUpId": FOLLOW,
    }


@pytest.mark.parametrize(
    "payload",
    [
        _pointer(followUpId="nope"),
        _pointer(roomId="nope"),
        _pointer(extra="x"),
        {k: v for k, v in _pointer().items() if k != "followUpId"},
    ],
)
def test_malformed_follow_up_pointer_is_rejected(payload):
    with pytest.raises(ValueError):
        deserialize_pointer(_record(payload))


def test_follow_up_run_id_is_distinct_from_the_original_run():
    original = messaging_repo.run_id_for(AGENT, TRIGGER)
    follow = messaging_repo.run_id_for(AGENT, f"{TRIGGER}:followup:{FOLLOW}")
    other = messaging_repo.run_id_for(AGENT, f"{TRIGGER}:followup:{FOLLOW[:-1]}4")
    assert len({original, follow, other}) == 3


def _handler_fixture(monkeypatch, *, row=ROW, meta=META, member=MEMBER, message=MESSAGE):
    metrics: list[tuple[str, dict]] = []
    monkeypatch.setattr(work, "put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    monkeypatch.setattr(work.messaging_repo, "get_follow_up", AsyncMock(return_value=row))
    monkeypatch.setattr(work.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=member))
    get_message = AsyncMock(return_value=message)
    monkeypatch.setattr(work.messaging_repo, "get_message_by_id", get_message)
    worker = OpenClawWorkWorker(sqs_client=MagicMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch_message", dispatch)
    return worker, dispatch, metrics, get_message


@pytest.mark.asyncio
async def test_handler_dispatches_against_the_trigger_message(monkeypatch):
    worker, dispatch, metrics, get_message = _handler_fixture(monkeypatch)
    await worker._room_follow_up({"kind": "room_follow_up", "roomId": ROOM, "agentId": AGENT, "followUpId": FOLLOW})
    get_message.assert_awaited_once_with(ROOM, TRIGGER)
    dispatch.assert_awaited_once_with(
        META,
        MEMBER,
        MESSAGE,
        "user_request",
        follow_up={"id": FOLLOW, "reason": "delegation_completed", "text": "review finished"},
    )
    assert ("messaging.followup.dispatched", {"dimensions": {"outcome": "dispatched"}}) in metrics


@pytest.mark.parametrize(
    ("piece", "outcome"),
    [("row", "expired"), ("meta", "room_gone"), ("member", "not_member"), ("message", "message_gone")],
)
@pytest.mark.asyncio
async def test_handler_drops_what_no_longer_exists(monkeypatch, piece, outcome):
    worker, dispatch, metrics, _ = _handler_fixture(monkeypatch, **{piece: None})
    await worker._room_follow_up({"kind": "room_follow_up", "roomId": ROOM, "agentId": AGENT, "followUpId": FOLLOW})
    dispatch.assert_not_awaited()
    assert ("messaging.followup.dispatched", {"dimensions": {"outcome": outcome}}) in metrics


def _dispatch_fixture(monkeypatch, *, listening=True):
    ledger: dict = {}
    posted: dict = {}

    async def fake_create_run(owner_id, run_id, **fields):
        ledger["run"] = (run_id, fields)
        return True

    async def fake_post(*args):
        posted["body"] = args[2]
        answer = MagicMock(status_code=202)
        answer.json.return_value = {"status": "accepted"}
        return answer

    async def fake_body(**kwargs):
        return {"version": 1, "eventId": kwargs["message"]["messageId"]}

    container = AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"})
    monkeypatch.setattr(work.container_repo, "get_by_owner_id", container)
    monkeypatch.setattr(work, "listening_available", AsyncMock(return_value=listening))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(work, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(work, "build_ingress_body", fake_body)
    monkeypatch.setattr(work.messaging_repo, "create_run", fake_create_run)
    monkeypatch.setattr(work, "put_metric", MagicMock())
    from core.services import local_sessions

    monkeypatch.setattr(local_sessions, "live_sessions_for_room", AsyncMock(return_value=[]))
    worker = OpenClawWorkWorker(sqs_client=MagicMock())
    monkeypatch.setattr(worker, "_post", fake_post)
    return worker, ledger, posted, container


@pytest.mark.asyncio
async def test_follow_up_ingress_carries_its_own_event_id_run_and_text(monkeypatch):
    worker, ledger, posted, _ = _dispatch_fixture(monkeypatch)
    follow_up = {"id": FOLLOW, "reason": "delegation_completed", "text": "review finished"}
    await worker._dispatch_message(META, MEMBER, MESSAGE, "user_request", follow_up=follow_up)

    run_id = messaging_repo.run_id_for(AGENT, f"{TRIGGER}:followup:{FOLLOW}")
    assert run_id != messaging_repo.run_id_for(AGENT, TRIGGER)
    assert posted["body"]["eventId"] == FOLLOW
    assert posted["body"]["followUp"] == follow_up
    assert posted["body"]["run"]["runId"] == run_id
    assert ledger["run"][0] == run_id
    assert ledger["run"][1]["trigger_message_id"] == TRIGGER and ledger["run"][1]["event_kind"] == "user_request"


@pytest.mark.asyncio
async def test_plain_dispatch_carries_no_follow_up(monkeypatch):
    worker, _, posted, _ = _dispatch_fixture(monkeypatch)
    await worker._dispatch_message(META, MEMBER, MESSAGE, "user_request")
    assert posted["body"]["eventId"] == TRIGGER and "followUp" not in posted["body"]
    assert posted["body"]["run"]["runId"] == messaging_repo.run_id_for(AGENT, TRIGGER)


@pytest.mark.asyncio
async def test_a_room_event_follow_up_is_paid_only(monkeypatch):
    worker, _, posted, container = _dispatch_fixture(monkeypatch, listening=False)
    follow_up = {"id": FOLLOW, "reason": "delegation_completed", "text": "x"}
    await worker._dispatch_message(META, MEMBER, MESSAGE, "room_event", follow_up=follow_up)
    container.assert_not_awaited()
    assert "body" not in posted


@pytest.mark.asyncio
async def test_follow_up_history_runs_through_the_rooms_latest_message(monkeypatch):
    """The follow-up turn must see its trigger and its own promise, both at/after message.messageSeq."""
    worker, _, _, _ = _dispatch_fixture(monkeypatch)
    seen: dict = {}

    async def capture_body(**kwargs):
        seen.update(kwargs)
        return {"version": 1, "eventId": kwargs["message"]["messageId"]}

    monkeypatch.setattr(work, "build_ingress_body", capture_body)
    follow_up = {"id": FOLLOW, "reason": "delegation_completed", "text": "x"}
    await worker._dispatch_message({**META, "lastSeq": 9}, MEMBER, MESSAGE, "user_request", follow_up=follow_up)
    assert seen["history_through_seq"] == 9
    seen.clear()
    await worker._dispatch_message({**META, "lastSeq": 9}, MEMBER, MESSAGE, "user_request")
    assert seen.get("history_through_seq") is None


@pytest.mark.asyncio
async def test_build_ingress_body_history_through_seq_includes_the_trigger(monkeypatch):
    before = AsyncMock(return_value=[])
    monkeypatch.setattr(work.messaging_repo, "list_messages_before", before)
    message = {
        **MESSAGE,
        "roomId": ROOM,
        "roomRevision": 1,
        "author": {"kind": "human", "id": "u1", "displayName": "U"},
        "text": "hi",
        "createdAt": "2026-09-22T00:00:00Z",
    }
    await work.build_ingress_body(
        meta=META, target_agent_id=AGENT, event_kind="user_request", message=message, members=[], history_through_seq=9
    )
    before.assert_awaited_once_with(ROOM, 10, work._HISTORY_LIMIT)
    before.reset_mock()
    await work.build_ingress_body(
        meta=META, target_agent_id=AGENT, event_kind="user_request", message=message, members=[]
    )
    before.assert_awaited_once_with(ROOM, 4, work._HISTORY_LIMIT)


@pytest.mark.asyncio
async def test_handler_drops_a_follow_up_into_an_archived_room(monkeypatch):
    worker, dispatch, metrics, _ = _handler_fixture(monkeypatch, meta={**META, "archivedAt": "2026-09-23T00:00:00Z"})
    await worker._room_follow_up({"kind": "room_follow_up", "roomId": ROOM, "agentId": AGENT, "followUpId": FOLLOW})
    dispatch.assert_not_awaited()
    assert ("messaging.followup.dispatched", {"dimensions": {"outcome": "archived"}}) in metrics


@pytest.mark.asyncio
async def test_room_dispatch_queued_before_the_archive_wakes_nobody_but_advances_the_cursor(monkeypatch):
    meta = {**META, "lastSeq": 4, "archivedAt": "2026-09-23T00:00:00Z"}
    message = {**MESSAGE, "dispatchTargets": [{"agentId": AGENT, "eventKind": "user_request"}]}
    monkeypatch.setattr(work.messaging_repo, "get_agent_delivery", AsyncMock(return_value={"lastSeq": 3}))
    monkeypatch.setattr(work.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(work.messaging_repo, "list_messages_between", AsyncMock(return_value=[message]))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    advance = AsyncMock(return_value=True)
    monkeypatch.setattr(work.messaging_repo, "advance_agent_delivery", advance)
    worker = OpenClawWorkWorker(sqs_client=MagicMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "_dispatch_message", dispatch)
    await worker._room_dispatch({"roomId": ROOM, "agentId": AGENT, "throughSeq": 4})
    dispatch.assert_not_awaited()
    advance.assert_awaited_once_with(ROOM, AGENT, 3, 4)
