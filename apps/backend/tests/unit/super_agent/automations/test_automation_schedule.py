"""Temporal Schedule wrapper: cron/tz validation and the Schedule built from an automation row (lane A, Task 4)."""

import uuid
from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy

from core.services import automation_schedule as asch
from core.temporal.automations import FireInput, ScheduleReconcileWorkflow


def _row(**over):
    return {
        "pk": "WS#org_1",
        "scheduleId": "s1",
        "cron": ["0 9 * * 1"],
        "tz": "America/New_York",
        "status": "active",
        "remainingRuns": None,
    } | over


@pytest.mark.parametrize(
    "cron",
    [
        ["0 9 * * * *"],  # 6 fields
        ["0 9 * *"],
        ["@hourly"],
        ["*/5 * * * *"],  # every 5 min
        ["* 9 * * *"],  # every minute of an hour
        ["0,5 9 * * *"],  # two fires 5 min apart
        ["0 9 * * *", "5 9 * * *"],  # 5 min apart across entries
        ["58 9 * * *", "3 10 * * *"],  # 5 min across the hour
        ["0 0 * * *", "55 23 * * *"],  # 5 min across midnight
        [],
        ["0 9 * * 1"] * 5,  # more than 4 entries
        ["61 9 * * *"],
        ["0 24 * * *"],
        ["x 9 * * *"],
        ["0 9 * * 1;rm"],
    ],
)
def test_cron_validation_rejects(cron):
    with pytest.raises(ValueError):
        asch.validate_cron(cron, "UTC")


@pytest.mark.parametrize(
    "cron",
    [
        ["*/10 * * * *"],
        ["0 9 * * 1"],
        ["0 9 * * MON-FRI"],
        ["0,30 9-17 * * 1-5"],
        ["0 9 1 JAN *"],
        ["0 9 * * *", "0 17 * * *"],
        ["10-50/20 * * * *"],
    ],
)
def test_cron_validation_accepts(cron):
    asch.validate_cron(cron, "UTC")


def test_tz_validation():
    asch.validate_cron(["0 9 * * 1"], "Europe/London")
    for tz in ("Mars/Olympus", "", "../../etc/passwd"):
        with pytest.raises(ValueError):
            asch.validate_cron(["0 9 * * 1"], tz)


def test_overlap_policy_is_skip():
    s = asch.build(_row())
    assert s.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert s.policy.catchup_window == timedelta(minutes=1)
    assert s.spec.cron_expressions == ["0 9 * * 1"]
    assert s.spec.time_zone_name == "America/New_York"


def test_action_fires_trigger_fire_workflow_with_ids_only():
    s = asch.build(_row())
    assert s.action.workflow == "TriggerFireWorkflow"
    assert s.action.id == "fire:org_1:s1"
    assert s.action.args == [FireInput(ws="org_1", schedule_id="s1", source="schedule")]


def test_one_off_remaining_runs():
    s = asch.build(_row(remainingRuns=1))
    assert (s.state.limited_actions, s.state.remaining_actions) == (True, 1)
    s = asch.build(_row())
    assert s.state.limited_actions is False


@pytest.mark.parametrize("status", ["draft", "paused", "paused_system"])
def test_paused_row_builds_paused_schedule(status):
    assert asch.build(_row(status=status)).state.paused is True
    assert asch.build(_row()).state.paused is False


def test_fingerprint_changes_with_cron_tz_and_runs_only():
    base = asch.fingerprint(_row())
    assert asch.fingerprint(_row(status="paused")) == base  # paused is compared from Temporal's state
    for change in ({"cron": ["0 10 * * 1"]}, {"tz": "UTC"}, {"remainingRuns": 1}):
        assert asch.fingerprint(_row(**change)) != base


async def test_reconcile_workflow_runs_the_activity(temporal_env):
    from temporalio import activity
    from temporalio.worker import Worker

    @activity.defn(name="reconcile_schedules")
    async def fake() -> dict:
        return {"created": 1, "updated": 0, "deleted_orphans": 0, "deleted_drafts": 0}

    queue = f"test-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=[ScheduleReconcileWorkflow], activities=[fake]):
        out = await temporal_env.client.execute_workflow(
            ScheduleReconcileWorkflow.run, id=f"r-{uuid.uuid4()}", task_queue=queue
        )
    assert out["created"] == 1
