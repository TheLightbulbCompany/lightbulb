"""TriggerFireWorkflow: a fire becomes one deterministic task that reads the previous run (lane A, Task 5)."""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from temporalio.worker import Worker

from core import tasks_schema as ts
from core.config import settings
from core.repositories import automations_repo as repo
from core.repositories import tasks_repo
from core.temporal.automations import (
    IDLE_DAYS,
    FireInput,
    TriggerFireWorkflow,
    close_automation_run,
    fire_automation,
    schedule_trigger_id,
)

WS = "org_1"
BUCKET = "owner-state-test"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


@pytest.fixture
def env(table, monkeypatch):
    """moto rows + S3, O's admission and task_conversation faked (the Router side is lane O's)."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
    enqueued: list[tuple[ts.Trigger, str | None]] = []

    async def enqueue(ws, trigger, *, room_id=None):
        item = trigger.model_dump(by_alias=True)
        _, created = await tasks_repo.put_trigger(ws, item)  # the real conditional put: retries dedupe here
        enqueued.append((trigger, room_id))
        return created

    results: dict[str, str] = {}
    container = {}  # the workspace's containers row: `last_interactive_at` feeds the idle skip

    async def get_container(ws):
        return container

    async def last_result(ws, task_id):
        return results.get(task_id)

    with (
        patch("core.services.admission.enqueue_trigger", side_effect=enqueue) as enq,
        patch("core.services.task_conversation.last_result_text", side_effect=last_result),
        patch("core.repositories.container_repo.get_by_owner_id", side_effect=get_container),
    ):
        yield type(
            "Env", (), {"s3": s3, "enqueued": enqueued, "enqueue": enq, "results": results, "container": container}
        )()


async def _active(sid="s1", **over):
    await repo.create_schedule(
        WS,
        {
            "scheduleId": sid,
            "name": "Monday signups",
            "ownerUserId": "user_sam",
            "cron": ["0 9 * * 1"],
            "tz": "America/New_York",
            "policyText": "Post the week's signups in #workshop-launch.",
            "budgetPerRunUsd": 1.0,
            "deliverTo": "room:r1",
        }
        | over,
    )
    return await repo.set_status(WS, sid, "active", reason=None, allowed_from=frozenset({"draft"}))


def _sched_input(sid="s1"):
    return FireInput(ws=WS, schedule_id=sid, source="schedule")


# --- trigger ids ------------------------------------------------------------------------------------


def test_trigger_id_uses_local_wall_clock_minute():
    # 2027-01-04T14:00Z is 09:00 in New York; the epoch is that of 09:00 read as UTC (S-04 ruling).
    assert schedule_trigger_id("s1", "America/New_York", _ms("2027-01-04T14:00:00+00:00")) == "sched:s1:1799053200"
    assert schedule_trigger_id("s1", "UTC", _ms("2027-01-04T14:00:00+00:00")) == "sched:s1:1799071200"


def test_fall_back_double_fire_is_one_trigger():
    # New York 2027-11-07: 01:30 EDT (05:30Z) and 01:30 EST (06:30Z) are one wall-clock minute.
    first = schedule_trigger_id("s1", "America/New_York", _ms("2027-11-07T05:30:00+00:00"))
    second = schedule_trigger_id("s1", "America/New_York", _ms("2027-11-07T06:30:00+00:00"))
    assert first == second
    # and the next day is a different one
    assert schedule_trigger_id("s1", "America/New_York", _ms("2027-11-08T06:30:00+00:00")) != first


# --- the Activity ------------------------------------------------------------------------------------


async def test_schedule_fire_starts_task_deterministically(env):
    await _active()
    at = _ms("2027-01-04T14:00:00+00:00")
    trigger_id = await fire_automation(_sched_input(), at)
    assert trigger_id == "sched:s1:1799053200"
    ((trigger, room_id),) = env.enqueued
    assert room_id == "r1"
    assert trigger.admission == "respond" and trigger.lane == "shared" and trigger.source == "schedule"
    assert trigger.actor_user_id == "user_sam"  # a fire has no person behind it: the automation's owner is the actor
    start = trigger.ref["start"]
    assert start["kind"] == "start_task" and start["triggerIds"] == [trigger_id]
    assert start["policyRef"] == "SCHED#s1" and start["budgetUsd"] == 1.0 and start["visibility"] == "room"
    assert start["title"] == "Monday signups" and start["brief"].startswith("Post the week's signups")
    assert "Previous run" not in start["brief"] and start["previousRunTaskId"] is None
    assert trigger.ref["taskId"] == ts.router_task_id(trigger_id, 0, 0)
    row = await repo.get_schedule(WS, "s1")
    assert row["activeRunTaskId"] == row["lastTaskId"] == trigger.ref["taskId"]


async def test_run_overlap_skipped(env):
    await _active()
    assert await repo.claim_run(WS, "s1", "t-running")
    assert await fire_automation(_sched_input(), _ms("2027-01-04T14:00:00+00:00")) == "skipped:overlap"
    assert env.enqueued == []


async def test_idle_workspace_skips_until_a_human_is_back(env):
    at = _ms("2027-01-04T14:00:00+00:00")
    await _active()
    env.container["last_interactive_at"] = at // 1000 - IDLE_DAYS * 86_400 - 1
    assert await fire_automation(_sched_input(), at) == "skipped:idle"
    row = await repo.get_schedule(WS, "s1")
    assert env.enqueued == [] and row.get("activeRunTaskId") is None and row["lastSkip"]["reason"] == "idle"
    manual = FireInput(ws=WS, schedule_id="s1", source="manual", dedupe_key="01HZZ", actor_user_id="user_sam")
    assert await fire_automation(manual, at) == "manual:s1:01HZZ"  # Run now has a person behind it
    assert "lastSkip" not in await repo.get_schedule(WS, "s1")  # a run supersedes the skip
    await repo.close_run(WS, "s1", env.enqueued[0][0].ref["taskId"], failed=False)
    hook = FireInput(ws=WS, schedule_id="s1", source="webhook", public_id="pub_1", dedupe_key="evt_1")
    assert await fire_automation(hook, at) == "hook:pub_1:evt_1"  # a real external event always fires
    await repo.close_run(WS, "s1", env.enqueued[1][0].ref["taskId"], failed=False)
    env.container["last_interactive_at"] = at // 1000 - 60  # someone sent a message: fires run again
    assert (await fire_automation(_sched_input(), at)).startswith("sched:")


async def test_paused_schedule_fire_is_noop(env):
    await _active()
    await repo.set_status(WS, "s1", "paused", reason=None, allowed_from=frozenset({"active"}))
    assert await fire_automation(_sched_input(), _ms("2027-01-04T14:00:00+00:00")) == "skipped:paused"
    assert await fire_automation(_sched_input("missing"), 0) == "skipped:paused"
    assert env.enqueued == [] and (await repo.get_schedule(WS, "s1")).get("activeRunTaskId") is None


async def test_previous_result_in_second_run_brief(env):
    await _active()
    first = await fire_automation(_sched_input(), _ms("2027-01-04T14:00:00+00:00"))
    first_task = env.enqueued[0][0].ref["taskId"]
    await tasks_repo.create_task(
        WS,
        {
            "taskId": first_task,
            "identifier": "LB-1",
            "title": "Monday signups",
            "status": "queued",
            "audience": {"kind": "room", "roomId": "r1"},
            "lane": "shared",
            "triggerId": first,
            "policyRef": "SCHED#s1",
        },
    )
    env.results[first_task] = "Lightbulb · Growth: 14 signups, 3 from the webinar. " + "x" * 5000
    await close_automation_run(WS, first_task, False)
    second = await fire_automation(_sched_input(), _ms("2027-01-11T14:00:00+00:00"))
    assert second != first
    start = env.enqueued[1][0].ref["start"]
    assert start["previousRunTaskId"] == first_task
    assert "Previous run (queued): Lightbulb · Growth: 14 signups" in start["brief"]
    assert len(start["brief"]) < len(env.results[first_task])  # capped at 4,000 chars of result


async def test_fire_activity_retry_is_idempotent(env):
    await _active()
    at = _ms("2027-01-04T14:00:00+00:00")
    assert await fire_automation(_sched_input(), at) == await fire_automation(_sched_input(), at)
    assert env.enqueue.await_count == 2
    assert [await tasks_repo.put_trigger(WS, env.enqueued[0][0].model_dump(by_alias=True))][0][1] is False
    row = await repo.get_schedule(WS, "s1")
    assert row["activeRunTaskId"] == env.enqueued[0][0].ref["taskId"]  # one claim, kept


async def test_webhook_fire_carries_payload_excerpt(env):
    await _active(deliverTo="none")
    key = f"{WS}/triggers/2027-01/pub_1/evt_1.json"
    env.s3.put_object(Bucket=BUCKET, Key=key, Body=b'{"id":"evt_1","type":"invoice.payment_failed"}' + b" " * 5000)
    inp = FireInput(ws=WS, schedule_id="s1", source="webhook", public_id="pub_1", dedupe_key="evt_1", payload_ref=key)
    assert await fire_automation(inp, 0) == "hook:pub_1:evt_1"
    ((trigger, room_id),) = env.enqueued
    assert room_id is None and trigger.ref["start"]["visibility"] == "person"
    assert trigger.ref["publicId"] == "pub_1" and trigger.ref["payloadRef"] == key
    excerpt = trigger.ref["start"]["brief"].split("Webhook payload:\n", 1)[1]
    assert excerpt.startswith('{"id":"evt_1"') and len(excerpt) <= 2000


async def test_manual_fire_uses_its_ulid(env):
    await _active()
    inp = FireInput(ws=WS, schedule_id="s1", source="manual", dedupe_key="01HZZ", actor_user_id="user_priya")
    assert await fire_automation(inp, 0) == "manual:s1:01HZZ"
    assert env.enqueued[0][0].actor_user_id == "user_priya"  # Run now: the person who pressed it, not the owner


# --- closing a run ------------------------------------------------------------------------------------


async def _task(task_id: str, policy_ref: str | None = "SCHED#s1"):
    await tasks_repo.create_task(
        WS,
        {
            "taskId": task_id,
            "identifier": "LB-9",
            "title": "run",
            "status": "queued",
            "audience": {"kind": "workspace"},
            "lane": "shared",
            "triggerId": "sched:s1:1",
            **({"policyRef": policy_ref} if policy_ref else {}),
        },
    )


async def test_breaker_pauses_after_three_failures_and_resets_on_success(env):
    await _active()
    with patch("core.services.automation_schedule.upsert", AsyncMock(return_value=None)) as upsert:
        for i, failed in enumerate([True, True, False, True, True]):
            tid = ts.new_task_id()
            await _task(tid)
            assert await repo.claim_run(WS, "s1", tid)
            await close_automation_run(WS, tid, failed)
            assert (await repo.get_schedule(WS, "s1"))["status"] == "active", i
        tid = ts.new_task_id()
        await _task(tid)
        await repo.claim_run(WS, "s1", tid)
        await close_automation_run(WS, tid, True)  # third in a row
    row = await repo.get_schedule(WS, "s1")
    assert (row["status"], row["pausedReason"], row["consecutiveFailures"]) == ("paused_system", "failures", 3)
    upsert.assert_awaited_once()


async def test_close_run_ignores_tasks_that_are_not_automation_runs(env):
    await _active()
    await _task("11111111-1111-4111-8111-111111111111", policy_ref=None)
    await close_automation_run(WS, "11111111-1111-4111-8111-111111111111", True)
    await close_automation_run(WS, "22222222-2222-4222-8222-222222222222", True)  # no such task
    assert (await repo.get_schedule(WS, "s1"))["consecutiveFailures"] == 0


async def test_close_run_on_stop_frees_the_claim(env):
    await _active()
    tid = ts.new_task_id()
    await _task(tid)
    await repo.claim_run(WS, "s1", tid)
    await close_automation_run(WS, tid, True)  # outcome cancelled → failed=True
    row = await repo.get_schedule(WS, "s1")
    assert "activeRunTaskId" not in row and row["runTaskIds"] == [tid]
    await close_automation_run(WS, tid, True)  # a retry after close is a no-op
    assert (await repo.get_schedule(WS, "s1"))["consecutiveFailures"] == 1


# --- the workflow (time-skipping env, the Activity faked) ------------------------------------------------


async def test_workflow_passes_scheduled_time_or_now(temporal_env):
    from temporalio import activity

    seen: list[tuple] = []

    @activity.defn(name="fire_automation")
    async def fake(inp: FireInput, at_ms: int) -> str:
        seen.append((inp, at_ms))
        return "sched:s1:1"

    queue = f"test-{uuid.uuid4()}"
    before = int((await temporal_env.get_current_time()).timestamp() * 1000)
    async with Worker(temporal_env.client, task_queue=queue, workflows=[TriggerFireWorkflow], activities=[fake]):
        out = await temporal_env.client.execute_workflow(
            TriggerFireWorkflow.run, _sched_input(), id=f"fire:{WS}:s1:{uuid.uuid4()}", task_queue=queue
        )
    assert out == "sched:s1:1"
    ((inp, at_ms),) = seen
    assert inp == _sched_input() and abs(at_ms - before) < 60_000  # no Schedule stamp → workflow.now()


# --- the TaskWorkflow hook (lane O's harness) -----------------------------------------------------------


async def _run_task(temporal_env, fakes, reason="router"):
    from datetime import timedelta

    from core.temporal.types import TaskStart
    from core.temporal.workflows import WORKFLOWS, TaskWorkflow

    queue = f"q-{uuid.uuid4()}"
    fast = timedelta(milliseconds=50)
    async with Worker(
        temporal_env.client,
        task_queue=queue,
        workflows=WORKFLOWS,
        activities=fakes.activities(),
        max_heartbeat_throttle_interval=fast,
        default_heartbeat_throttle_interval=fast,
    ):
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, reason), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await h.result()


TID = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


@pytest.mark.parametrize("outcome,failed", [("completed", False), ("failed", True), ("cancelled", False)])
async def test_task_workflow_closes_the_run_after_an_ended_attempt(temporal_env, outcome, failed):
    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    from core.temporal.types import WorkerTurnOut

    fakes = Fakes()
    fakes.behaviour["worker_turn"] = lambda inp: WorkerTurnOut(outcome)
    await _run_task(temporal_env, fakes)
    names = fakes.names()
    assert names.index("close_automation_run") == names.index("end_attempt") + 1
    assert fakes.args("close_automation_run") == [(WS, TID, failed)]


async def test_task_workflow_keeps_the_run_open_while_the_task_goes_on(temporal_env):
    """`runtime_lost` leaves the task working (O retries the attempt): the run closes after the retry, once."""
    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    from core.temporal.types import WorkerTurnOut

    fakes = Fakes()
    turns = iter([WorkerTurnOut("runtime_lost"), WorkerTurnOut("completed")])
    fakes.behaviour["worker_turn"] = lambda inp: next(turns)
    await _run_task(temporal_env, fakes)
    assert fakes.names().count("end_attempt") == 2
    assert fakes.args("close_automation_run") == [(WS, TID, False)]


# --- paths that end the task without ending an attempt (review of #4748) ------------------------------------


async def _run_until(temporal_env, fakes, drive):
    """Start a TaskWorkflow on O's harness, let `drive(handle)` push it, return when it completes."""
    from datetime import timedelta

    from core.temporal.types import TaskStart
    from core.temporal.workflows import WORKFLOWS, TaskWorkflow

    queue = f"q-{uuid.uuid4()}"
    fast = timedelta(milliseconds=50)
    async with Worker(
        temporal_env.client,
        task_queue=queue,
        workflows=WORKFLOWS,
        activities=fakes.activities(),
        max_heartbeat_throttle_interval=fast,
        default_heartbeat_throttle_interval=fast,
    ):
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await drive(h)
        await h.result()


async def test_lost_runtime_three_times_closes_the_run_as_failed(temporal_env):
    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    from core.temporal.types import WorkerTurnOut

    fakes = Fakes()
    fakes.behaviour["worker_turn"] = lambda inp: WorkerTurnOut("runtime_lost")

    async def nothing(h):
        return None

    await _run_until(temporal_env, fakes, nothing)
    # failed after the third loss, then the 7-day quiet resolve: the run closed once, as a failure
    assert fakes.args("transition")[0] == (WS, TID, "failed", "runtime_lost")
    assert fakes.args("close_automation_run") == [(WS, TID, True)]


async def test_stop_while_queued_closes_the_run(temporal_env):
    import asyncio

    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    from core.temporal.types import StopRequest

    fakes = Fakes()
    fakes.behaviour["acquire_slot"] = lambda ws, tid: False  # no slot: the task waits, queued

    async def stop(h):
        for _ in range(100):
            if "acquire_slot" in fakes.names():
                break
            await asyncio.sleep(0.05)
        await h.execute_update("stop", StopRequest(by_user_id="sam"))

    await _run_until(temporal_env, fakes, stop)
    assert fakes.args("transition") == [(WS, TID, "resolved", "stopped")]
    assert fakes.args("close_automation_run") == [(WS, TID, False)]  # freed, and not a failure


async def test_dismiss_closes_the_run(temporal_env):
    import asyncio

    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    from core.temporal.types import ActionRequest

    fakes = Fakes()
    fakes.behaviour["acquire_slot"] = lambda ws, tid: False

    async def dismiss(h):
        for _ in range(100):
            if "acquire_slot" in fakes.names():
                break
            await asyncio.sleep(0.05)
        await h.execute_update("dismiss", ActionRequest("sam"))

    await _run_until(temporal_env, fakes, dismiss)
    assert fakes.args("transition") == [(WS, TID, "resolved", "dismissed")]
    assert fakes.args("close_automation_run") == [(WS, TID, False)]


async def test_quiet_resolve_after_a_closed_attempt_closes_once(temporal_env):
    from tests.unit.super_agent.orchestration.fake_activities import Fakes

    fakes = Fakes()

    async def nothing(h):
        return None

    await _run_until(temporal_env, fakes, nothing)  # completed → idle → 7-day quiet → resolved
    assert fakes.args("transition") == [(WS, TID, "resolved", "quiet")]
    assert fakes.args("close_automation_run") == [(WS, TID, False)]  # once: after the attempt, not again


@pytest.mark.parametrize("user,room", [("user_sam", "dm-sam"), ("user_ada", None)])
async def test_dm_delivery_resolves_the_owners_dm_with_lightbulb(env, user, room):
    """Audit item 8: `dm:{owner}` answers in the owner's DM with the front agent; a dm: naming anyone else is not
    delivered. Either way the run stays person-visible."""
    await _active(deliverTo=f"dm:{user}")
    dm = AsyncMock(return_value="dm-sam")
    with (
        patch("core.services.messaging._agent_dm_room_id", dm),
        patch("core.services.task_conversation.front_agent_id", AsyncMock(return_value="agent_ceo")),
    ):
        await fire_automation(_sched_input(), _ms("2027-01-04T14:00:00+00:00"))
    ((trigger, room_id),) = env.enqueued
    assert room_id == room and trigger.ref["start"]["visibility"] == "person"
    if room:
        dm.assert_awaited_once_with(WS, "user_sam", "agent_ceo")
