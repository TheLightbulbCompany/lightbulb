"""Inbound A2A → a pinned task (wave2-Q Task 10): idempotent per A2A task, private lane, pinned specialist,
answered in the requester's DM; a stopped task is never restarted by a late allow; off means nothing happens."""

from unittest.mock import AsyncMock

import pytest

from core import tasks_schema as ts
from core.repositories import tasks_repo
from core.services import a2a_admission, admission, hosted_mcp, super_agent_flags

from .conftest import TASK, WS

ROW = {
    "id": "a2a-task-1",
    "agentId": "agent-finance",
    "requester": "user_sam",
    "text": "Send Acme the Q3 invoice.\nThanks.",
}


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(hosted_mcp, "person_dm", AsyncMock(return_value="room-dm-1"))
    admits = []

    async def put_admit(ws, trigger_id):
        admits.append((ws, trigger_id))

    monkeypatch.setattr(admission.messaging_repo, "put_admit_trigger", put_admit)
    monkeypatch.setattr(a2a_admission, "put_metric", lambda *a, **k: None)
    return {"admits": admits}


async def test_inbound_lands_in_private_lane_pinned(world):
    trigger_id = await a2a_admission.admit_inbound_a2a(WS, ROW)
    assert trigger_id == "a2a:a2a-task-1"
    row = await tasks_repo.get_trigger(WS, trigger_id)
    assert row["source"] == "a2a" and row["lane"] == ts.router_lane_key("user_sam") and row["actorUserId"] == "user_sam"
    assert row["ref"]["deliverRoomId"] == "room-dm-1" and row["ref"]["a2aTaskId"] == "a2a-task-1"
    start = row["ref"]["start"]
    assert (start["kind"], start["specialist"], start["visibility"]) == ("start_task", "agent-finance", "person")
    assert start["title"] == "Send Acme the Q3 invoice. Thanks." and start["brief"] == ROW["text"]
    assert start["a2aTaskId"] == "a2a-task-1"  # the one wire name, same as Task.a2a_task_id's alias
    assert world["admits"] == [(WS, trigger_id)]
    hosted_mcp.person_dm.assert_awaited_once()
    assert hosted_mcp.person_dm.await_args.kwargs == {"create_key": "a2a-task-1"}


async def test_admission_idempotent_per_a2a_task(world):
    first = await a2a_admission.admit_inbound_a2a(WS, ROW)
    second = await a2a_admission.admit_inbound_a2a(WS, {**ROW, "text": "changed"})
    assert first == second
    assert (await tasks_repo.get_trigger(WS, first))["ref"]["text"] == ROW["text"]  # the first request stands
    # The admit row is re-put every time (a crash between the two puts never strands the request); the outbox
    # ignores an identical re-put, so the Router still sees it once.
    assert len(world["admits"]) == 2 and hosted_mcp.person_dm.await_count == 2


async def test_stopped_task_not_restarted_on_late_allow(world):
    trigger_id = await a2a_admission.admit_inbound_a2a(WS, ROW)
    # The Router started the task and a person stopped it.
    from core.dynamodb import get_table

    get_table("tasks").update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.trigger_sk(trigger_id)},
        UpdateExpression="SET routedTo = :r",
        ExpressionAttributeValues={":r": {"taskId": TASK}},
    )
    get_table("tasks").update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(TASK)},
        UpdateExpression="SET #s = :s, statusReason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "resolved", ":r": "stopped"},
    )
    assert await a2a_admission.admit_inbound_a2a(WS, ROW) is None
    assert len(world["admits"]) == 1


async def test_switch_off_writes_nothing(world):
    super_agent_flags.flags_for.return_value = False
    assert await a2a_admission.admit_inbound_a2a(WS, ROW) is None
    assert await tasks_repo.get_trigger(WS, "a2a:a2a-task-1") is None and world["admits"] == []
    hosted_mcp.person_dm.assert_not_awaited()


async def test_report_a2a_is_pending_until_governance_lands(world, monkeypatch):
    metrics = []
    monkeypatch.setattr(a2a_admission, "put_metric", lambda name, **kw: metrics.append((name, kw)))
    await a2a_admission.report_a2a(WS, TASK, "completed")  # the seeded task has no A2A id
    assert metrics == []
    # The key O11 writes when it starts an A2A task (activities.create_task: "a2aTaskId"), through the Task model.
    row, _ = await tasks_repo.create_task(
        WS,
        {
            "taskId": "1f14e45f-ceea-467a-9575-8a1b2c3d4e5f",
            "identifier": "LB-2",
            "title": "From Acme",
            "status": "queued",
            "audience": {"kind": "person", "userIds": ["user_sam"]},
            "lane": "private",
            "triggerId": "a2a:a2a-task-1",
            "a2aTaskId": "a2a-task-1",
        },
    )
    assert row["a2aTaskId"] == "a2a-task-1"
    await a2a_admission.report_a2a(WS, "1f14e45f-ceea-467a-9575-8a1b2c3d4e5f", "completed")
    assert metrics == [("a2a.report_pending", {"dimensions": {"outcome": "completed"}})]
