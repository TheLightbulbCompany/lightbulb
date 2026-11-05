"""TaskWorkflow's reconcile start (wave2-W Task 6, spec 11 §3.6): the watchdog's only action. A working
orphan is failed `lost_run` and never re-attempted; a queued orphan is restarted normally (never failed);
any attempt left open is closed as `runtime_lost` first."""

import uuid

import pytest
from temporalio.worker import Worker

from core.temporal.types import TaskStart
from core.temporal.workflows import WORKFLOWS, TaskWorkflow

from .fake_activities import Fakes

WS, TID = "org_1", "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


async def _reconcile(env, state: str) -> Fakes:
    fakes = Fakes()
    fakes.behaviour["reconcile_state"] = lambda ws, t: state
    queue = f"q-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        await env.client.execute_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "reconcile"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
    return fakes


async def test_taskworkflow_reconcile_reason_never_starts_attempt(temporal_env):
    fakes = await _reconcile(temporal_env, "working:2:live")
    assert fakes.names() == ["reconcile_state", "end_attempt", "transition", "close_automation_run"]
    assert fakes.args("end_attempt") == [(WS, TID, 2, "runtime_lost", "lost_run")]
    assert fakes.args("transition") == [(WS, TID, "failed", "lost_run")]


async def test_reconcile_on_already_ended_attempt_only_transitions(temporal_env):
    fakes = await _reconcile(temporal_env, "working:2:ended")
    assert fakes.names() == ["reconcile_state", "release_slot", "transition", "close_automation_run"]
    assert fakes.args("transition") == [(WS, TID, "failed", "lost_run")]


async def test_reconcile_queued_without_attempt_restarts_normally(temporal_env):
    fakes = await _reconcile(temporal_env, "queued:0:ended")
    names = fakes.names()
    assert names[:5] == ["reconcile_state", "check_budget", "acquire_slot", "begin_attempt", "worker_turn"]
    assert "lost_run" not in [a[3] for a in fakes.args("transition")]
    assert fakes.args("end_attempt")[0][3] == "completed"


async def test_reconcile_queued_closes_open_attempt_before_the_new_one(temporal_env):
    fakes = await _reconcile(temporal_env, "queued:1:live")
    names = fakes.names()
    assert names[:3] == ["reconcile_state", "end_attempt", "check_budget"]
    assert fakes.args("end_attempt")[0] == (WS, TID, 1, "runtime_lost", "lost_run")
    assert names.index("begin_attempt") > names.index("end_attempt")
    assert "lost_run" not in [a[3] for a in fakes.args("transition")]


@pytest.mark.parametrize("state", ["other"])
async def test_reconcile_on_a_task_that_moved_on_does_nothing(temporal_env, state):
    fakes = await _reconcile(temporal_env, state)
    assert fakes.names() == ["reconcile_state"]


async def test_reconcile_state_activity(monkeypatch):
    from unittest.mock import AsyncMock

    from core.services import watchdog as wd

    cases = [
        ({"status": "working", "attemptCount": 2}, {"status": "running"}, "working:2:live"),
        ({"status": "working", "attemptCount": 2}, {"status": "ended"}, "working:2:ended"),
        ({"status": "queued", "attemptCount": 0}, None, "queued:0:ended"),
        ({"status": "idle", "attemptCount": 1}, None, "other"),
        ({"status": "working", "attemptCount": 1, "stopRequestedBy": "user_x"}, {"status": "running"}, "other"),
        (None, None, "other"),
    ]
    for task, att, want in cases:
        monkeypatch.setattr(wd.tasks_repo, "get_task", AsyncMock(return_value=task))
        monkeypatch.setattr(wd.tasks_repo, "get_attempt", AsyncMock(return_value=att))
        assert await wd.reconcile_state(WS, TID) == want
