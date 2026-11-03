"""Delegation follow-ups: the FOLLOWUP row and its WORK# outbox pointer land together, once."""

import time

import pytest

from core.repositories import messaging_repo as repo
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, _room, table  # noqa: F401

FOLLOW = "33333333-3333-4333-8333-333333333333"
TRIGGER = "44444444-4444-4444-8444-444444444444"


async def _create(room_id, follow_up_id=FOLLOW, agent_id=FRIDAY):
    return await repo.create_follow_up(
        owner_id=OWNER,
        room_id=room_id,
        agent_id=agent_id,
        follow_up_id=follow_up_id,
        trigger_message_id=TRIGGER,
        event_kind="user_request",
        reason="delegation_completed",
        text="review done",
    )


@pytest.mark.asyncio
async def test_follow_up_and_its_work_pointer_are_written_together(table):  # noqa: F811
    room_id, _ = await _room()
    assert await _create(room_id) is True

    row = await repo.get_follow_up(room_id, FOLLOW)
    assert row["agentId"] == FRIDAY and row["triggerMessageId"] == TRIGGER
    assert row["eventKind"] == "user_request" and row["text"] == "review done"
    assert row["expiresAt"] > time.time() + 6 * 24 * 3600

    work = table.get_item(Key={"pk": repo.room_pk(room_id), "sk": f"WORK#FOLLOWUP#{FRIDAY}#{FOLLOW}"})["Item"]
    # The Pipe filter is ``sk`` prefix ``WORK#`` -- this is the row that reaches the FIFO.
    assert work["sk"].startswith("WORK#")
    assert work["payload"] == {"kind": "room_follow_up", "roomId": room_id, "agentId": FRIDAY, "followUpId": FOLLOW}
    group, dedup = repo.room_fifo_ids(OWNER, room_id, FRIDAY, FOLLOW)
    assert work["messageGroupId"] == group and work["messageDeduplicationId"] == dedup


@pytest.mark.asyncio
async def test_a_second_write_of_the_same_follow_up_is_a_replay(table):  # noqa: F811
    room_id, _ = await _room()
    assert await _create(room_id) is True
    before = table.scan()["Items"]
    assert await _create(room_id) is False
    assert table.scan()["Items"] == before


@pytest.mark.asyncio
async def test_recent_follow_ups_count_per_agent_since_a_point(table):  # noqa: F811
    room_id, _ = await _room()
    await _create(room_id, "33333333-3333-4333-8333-000000000001")
    await _create(room_id, "33333333-3333-4333-8333-000000000002")
    await _create(room_id, "33333333-3333-4333-8333-000000000003", agent_id="99999999-9999-4999-8999-999999999999")

    assert await repo.count_follow_ups_since(room_id, FRIDAY, None) == 2
    assert await repo.count_follow_ups_since(room_id, FRIDAY, "9999-01-01") == 0
