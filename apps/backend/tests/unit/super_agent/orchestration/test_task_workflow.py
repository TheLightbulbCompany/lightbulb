"""TaskWorkflow with fake Activities on the time-skipping server (O Task 2; review focus 3)."""

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from core.temporal import workflows as wf

from core.temporal.types import ActionRequest, StopRequest, TaskInputRef, TaskStart, WorkerTurnOut
from core.temporal.workflows import WORKFLOWS, TaskWorkflow

from .fake_activities import Fakes, go_async, until_cancelled

WS, TID = "org_1", "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


@pytest.fixture
def fakes():
    return Fakes()


@contextlib.asynccontextmanager
async def worker(env, fakes, **kw):
    queue = f"q-{uuid.uuid4()}"
    fast = timedelta(milliseconds=50)  # cancellation reaches an activity on its next heartbeat flush
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=WORKFLOWS,
        activities=fakes.activities(),
        max_heartbeat_throttle_interval=fast,
        default_heartbeat_throttle_interval=fast,
        **kw,
    ):
        yield queue


async def _start(env, queue, reason="router", wid=None):
    return await env.client.start_workflow(
        TaskWorkflow.run, TaskStart(WS, TID, reason), id=wid or f"task:{WS}:{uuid.uuid4()}", task_queue=queue
    )


async def _until(pred, n=200):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_happy_path_attempt(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await h.result()
    assert fakes.names() == [
        "check_budget",
        "acquire_slot",
        "begin_attempt",
        "worker_turn",
        "end_attempt",
        "transition",
    ]
    assert fakes.args("end_attempt")[0][3] == "completed"
    assert fakes.args("transition") == [(WS, TID, "resolved", "quiet")]


async def test_proposed_start(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q, reason="migrated")
        await asyncio.sleep(0.3)
        assert fakes.names() == []
        await h.signal(TaskWorkflow.start)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.terminate()


async def test_proposed_dismiss(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q, reason="migrated")
        await h.execute_update(TaskWorkflow.dismiss, ActionRequest("sam", 1))
        await h.result()
    assert fakes.names() == ["transition"]
    assert fakes.args("transition") == [(WS, TID, "resolved", "dismissed")]


async def test_input_during_attempt_runs_next_attempt(temporal_env, fakes):
    gate = asyncio.Event()

    async def first_waits(inp):
        if inp.n == 1:
            await gate.wait()
        return WorkerTurnOut("completed")

    fakes.behaviour["worker_turn"] = first_waits
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "worker_turn" in fakes.names())
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:m2", "message"))
        await _until(lambda: "deliver_input" in fakes.names())
        gate.set()
        await _until(lambda: len(fakes.args("begin_attempt")) == 2)
        await h.terminate()
    assert fakes.args("deliver_input")[0] == (WS, TID, 1, "chat:m2")
    assert fakes.args("begin_attempt")[1][2] == ["chat:m2"]


async def test_stop_while_working(temporal_env, fakes):
    fakes.behaviour["worker_turn"] = until_cancelled
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "worker_turn" in fakes.names())
        res = await h.execute_update(TaskWorkflow.stop, StopRequest("sam"))
        await h.result()
    assert res.status == "stopping"
    names = fakes.names()
    assert names.index("mark_stop_requested") < names.index("end_attempt")
    assert fakes.args("end_attempt")[0][3] == "cancelled"
    assert "transition" not in names  # end_attempt(cancelled) writes resolved/stopped


async def test_stop_calls_on_stop_hook_before_cancel(temporal_env, fakes):
    fakes.behaviour["worker_turn"] = until_cancelled
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "worker_turn" in fakes.names())
        await h.execute_update(TaskWorkflow.stop, StopRequest("sam"))
        await h.result()
    # the default hook is empty; Q's override runs at this point: before mark_stop_requested
    assert fakes.names()[fakes.names().index("worker_turn") + 1] == "mark_stop_requested"


async def test_slot_wait_released_by_signal(temporal_env, fakes):
    grants = iter([False, True])
    fakes.behaviour["acquire_slot"] = lambda *a: next(grants)
    fakes.behaviour["worker_turn"] = until_cancelled  # keeps the turn open: no idle-wait timer afterwards
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: len(fakes.args("acquire_slot")) == 1)
        await asyncio.sleep(0.3)
        assert len(fakes.args("acquire_slot")) == 1  # no timer re-polls
        await h.signal(TaskWorkflow.slot_free)
        await _until(lambda: "worker_turn" in fakes.names())
        await h.terminate()
    history = await h.fetch_history()
    assert not any(e.HasField("timer_started_event_attributes") for e in history.events)


@pytest.fixture
def short_heartbeat(monkeypatch):
    """The time-skipping server keeps time locked while an async-completed activity is outstanding, so a
    lost turn is simulated with a 1 s real heartbeat timeout (unsandboxed, so the patch reaches the workflow)."""
    monkeypatch.setitem(wf._WORKER_TURN, "heartbeat_timeout", timedelta(seconds=1))
    return {"workflow_runner": UnsandboxedWorkflowRunner()}


async def test_worker_lost_reconciles_same_run_id(temporal_env, fakes, short_heartbeat):
    turns = iter([go_async, lambda *a: WorkerTurnOut("completed")])
    fakes.behaviour["worker_turn"] = lambda *a: next(turns)(*a)
    fakes.behaviour["reconcile_attempt"] = lambda *a: "running"
    async with worker(temporal_env, fakes, **short_heartbeat) as q:
        h = await _start(temporal_env, q)
        await h.result()
    runs = [a[0].run_id for a in fakes.args("worker_turn")]
    assert runs == ["run-1", "run-1"]
    assert len(fakes.args("begin_attempt")) == 1 and len(fakes.args("end_attempt")) == 1


async def test_runtime_lost_new_attempt_then_failed(temporal_env, fakes, short_heartbeat):
    fakes.behaviour["worker_turn"] = go_async
    fakes.behaviour["reconcile_attempt"] = lambda *a: "lost"
    async with worker(temporal_env, fakes, **short_heartbeat) as q:
        h = await _start(temporal_env, q)
        await h.result()
    assert [a[3] for a in fakes.args("end_attempt")] == ["runtime_lost"] * 3
    assert fakes.args("transition")[0] == (WS, TID, "failed", "runtime_lost")


async def test_reopen_after_resolve_reuses_workflow_id(temporal_env, fakes):
    wid = f"task:{WS}:{TID}"
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q, reason="migrated", wid=wid)
        await h.execute_update(TaskWorkflow.dismiss, ActionRequest("sam"))
        await h.result()
        op = WithStartWorkflowOperation(
            TaskWorkflow.run,
            TaskStart(WS, TID, "migrated"),
            id=wid,
            task_queue=q,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        await temporal_env.client.execute_update_with_start_workflow(
            TaskWorkflow.reopen, ActionRequest("sam"), start_workflow_operation=op
        )
        h2 = await op.workflow_handle()
        await _until(lambda: "end_attempt" in fakes.names())
        assert h2.id == wid and h2.result_run_id != h.result_run_id
        await h2.terminate()
    assert len(fakes.args("begin_attempt")) == 1
