"""Room ingestion (wave2-M Task 4): the one writer of room text, scoped by who may read the room."""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from core.repositories import messaging_repo
from core.services import room_memory

OWNER = "org_1"
ROOM = "3f1c2a8e-1111-4c1e-9d55-7f3a2b1c0e99"
MSG = "9a8b7c6d-2222-4c1e-9d55-7f3a2b1c0e99"
TASK = "0b6c0d2e-4a8f-4c1e-9d55-7f3a2b1c0e99"
SAM = {"kind": "human", "id": "user_sam"}
PRIYA = {"kind": "human", "id": "user_priya"}
LB = {"kind": "agent", "id": "agent-ceo"}


class FakeAgentCore:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[dict] = []
        self.records: list[dict] = []
        self.fail = fail

    def create_event(self, **kw):
        if self.fail:
            raise RuntimeError("throttled")
        self.events.append(kw)
        return {"event": {"eventId": f"ev{len(self.events)}"}}

    def batch_create_memory_records(self, *, memoryId, records, clientToken):
        self.records.extend(records)
        ok = [
            {"requestIdentifier": r["requestIdentifier"], "memoryRecordId": "r", "status": "SUCCEEDED"} for r in records
        ]
        return {"successfulRecords": ok, "failedRecords": []}


@pytest.fixture
def table():
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="msg",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch.object(messaging_repo, "_table", return_value=t):
            yield t


@pytest.fixture
def core(monkeypatch):
    fake = FakeAgentCore()
    monkeypatch.setattr(room_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    monkeypatch.setattr(room_memory.workspace_memory, "_client", lambda: fake)
    monkeypatch.setattr(room_memory, "flags_for", AsyncMock(return_value=True))
    return fake


def _room(kind="channel", members=(SAM, PRIYA, LB), **extra) -> dict:
    return {"roomId": ROOM, "ownerId": OWNER, "kind": kind, "members": list(members), **extra}


def _msg(text="Ship the Acme deck Friday", author=None, **extra) -> dict:
    return {
        "messageId": MSG,
        "roomId": ROOM,
        "author": author or {**SAM, "displayName": "Sam"},
        "text": text,
        "threadRootMessageId": None,
        "createdAt": "2026-11-04T10:00:00+00:00",
        **extra,
    }


def _meta(kw: dict) -> dict:
    return {k: v["stringValue"] for k, v in kw["metadata"].items()}


async def test_channel_message_writes_team_event_with_tags(table, core):
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is True
    [ev] = core.events
    assert ev["actorId"] == OWNER and ev["sessionId"] == f"room-{ROOM}-top"
    assert ev["payload"][0]["conversational"] == {"content": {"text": "Sam: Ship the Acme deck Friday"}, "role": "USER"}
    assert _meta(ev) == {"threadId": f"room:{ROOM}:{MSG}", "roomId": ROOM, "lane": "shared", "source": "room"}
    [rec] = core.records
    assert rec["namespaces"] == ["/turns/org_1/"]
    assert {k: v["stringValue"] for k, v in rec["metadata"].items()} == {
        **_meta(ev),
        "speaker": "Sam",
        "session": f"room-{ROOM}-top",
    }


async def test_thread_reply_keys_on_its_root(table, core):
    await room_memory.ingest_message(OWNER, _room(), _msg(threadRootMessageId="root-1"))
    [ev] = core.events
    assert ev["sessionId"] == f"room-{ROOM}-root-1" and _meta(ev)["threadId"] == f"room:{ROOM}:root-1"


async def test_dm_with_agent_writes_private_actor_and_private_turn(table, core):
    reply = _msg(text="Noted.", author={**LB, "displayName": "Lightbulb"})
    assert await room_memory.ingest_message(OWNER, _room("dm", (SAM, LB)), reply) is True
    [ev] = core.events
    assert ev["actorId"] == "org_1:user_sam" and _meta(ev)["lane"] == "private"
    assert ev["payload"][0]["conversational"]["role"] == "ASSISTANT"
    assert core.records[0]["namespaces"] == ["/turns/org_1:user_sam/"]


async def test_dm_between_people_writes_nothing(table, core):
    assert await room_memory.ingest_message(OWNER, _room("dm", (SAM, PRIYA)), _msg()) is False
    assert core.events == [] and core.records == []


async def test_group_dm_without_an_agent_writes_nothing(table, core):
    assert await room_memory.ingest_message(OWNER, _room("group-dm", (SAM, PRIYA)), _msg()) is False
    assert core.events == [] and core.records == []


async def test_group_chat_with_lightbulb_writes_its_own_scope_never_the_team(table, core):
    """Founder ruling (6 Nov 2026): remembered, but only for that chat's members."""
    assert await room_memory.ingest_message(OWNER, _room("group-dm"), _msg()) is True
    [ev] = core.events
    assert ev["actorId"] == f"org_1:room_{ROOM}" and _meta(ev)["lane"] == "room"
    assert core.records[0]["namespaces"] == [f"/turns/org_1:room_{ROOM}/"]


async def test_a_group_chat_tasks_room_writes_the_chats_scope(table, core):
    task = {"taskId": TASK, "lane": "shared", "audience": {"kind": "room", "roomId": ROOM}}
    home = _room("group-dm")
    with patch.object(room_memory.messaging_repo, "get_room", AsyncMock(return_value=home)):
        room = {**_room(members=(LB,), task={"taskId": TASK}), "roomId": "task:" + TASK}
        assert await room_memory.ingest_message(OWNER, room, _msg(), task=task) is True
    assert core.events[0]["actorId"] == f"org_1:room_{ROOM}"


async def test_another_workspaces_group_chat_is_never_a_scope(table, core):
    assert await room_memory.group_room("org_2", _room("group-dm")) is None


async def test_network_and_a2a_messages_never_written(table, core):
    a2a = {"kind": "system", "id": "a2a:user_x", "displayName": "A2A via user_x"}
    assert await room_memory.ingest_message(OWNER, _room("dm", (SAM, LB)), _msg(author=a2a)) is False
    assert await room_memory.ingest_message(OWNER, _room(), _msg(progress=True)) is False
    assert core.events == []


async def test_private_task_room_writes_owner_private_actor(table, core):
    task = {"taskId": TASK, "lane": "private", "ownerUserId": "user_priya"}
    room = _room(members=(LB,), task={"taskId": TASK})
    assert await room_memory.ingest_message(OWNER, room, _msg(), task=task) is True
    [ev] = core.events
    assert ev["actorId"] == "org_1:user_priya" and _meta(ev)["taskId"] == TASK
    assert core.records[0]["namespaces"] == ["/turns/org_1:user_priya/"]


async def test_task_room_reads_its_task_when_not_given(table, core):
    get = AsyncMock(return_value={"taskId": TASK, "lane": "shared"})
    with patch.object(room_memory.tasks_repo, "get_task", get):
        assert await room_memory.ingest_message(OWNER, _room(task={"taskId": TASK}), _msg()) is True
    get.assert_awaited_once_with(OWNER, TASK)
    assert core.events[0]["actorId"] == OWNER and _meta(core.events[0])["taskId"] == TASK


async def test_task_id_tag_from_subscription_at_ingest(table, core):
    await room_memory.ingest_message(OWNER, _room(), _msg(), task={"taskId": TASK, "lane": "shared"})
    assert _meta(core.events[0])["taskId"] == TASK


async def test_secret_is_redacted_before_event_and_record(table, core):
    key = "sk-" + "a" * 30
    await room_memory.ingest_message(OWNER, _room(), _msg(text=f"use {key} for the deploy"))
    text = core.events[0]["payload"][0]["conversational"]["content"]["text"]
    assert key not in text and "[REDACTED]" in text
    assert core.records[0]["content"]["text"] == text


async def test_ingest_is_idempotent_on_message_id(table, core):
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is True
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is True
    assert len(core.events) == 1 and len(core.records) == 1
    assert core.events[0]["clientToken"] == core.records[0]["requestIdentifier"]


async def test_ingest_failure_never_raises(table, core, caplog):
    core.fail = True
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is False
    assert "RuntimeError" in caplog.text and "Ship the Acme" not in caplog.text
    core.fail = False  # not marked done: the redelivery writes it
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is True


async def test_switch_off_ingests_nothing(table, core, monkeypatch):
    monkeypatch.setattr(room_memory, "flags_for", AsyncMock(return_value=False))
    assert await room_memory.ingest_message(OWNER, _room(), _msg()) is False
    assert core.events == []


async def test_members_read_when_meta_has_none(table, core):
    room = _room("dm")
    del room["members"]
    with patch.object(room_memory.messaging_repo, "list_members", AsyncMock(return_value=[SAM, LB])):
        assert await room_memory.ingest_message(OWNER, room, _msg()) is True
    assert core.events[0]["actorId"] == "org_1:user_sam"
