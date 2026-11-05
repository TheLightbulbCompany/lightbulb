"""Client helpers every lane uses to reach a task or the Router (wave-2 O Task 1, contracts §7, G24)."""

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from core.temporal import client as tc
from core.temporal.types import ActionRequest, TaskInputRef, TaskStart, TriggerRef


class FakeClient:
    def __init__(self):
        self.calls = []

    async def start_workflow(self, workflow, arg, **kw):
        self.calls.append(("start_workflow", workflow, arg, kw))

    async def execute_update_with_start_workflow(self, update, arg, *, start_workflow_operation, **kw):
        self.calls.append(("update_with_start", update, arg, start_workflow_operation))
        return "ok"

    def get_workflow_handle(self, wid, **kw):
        self.calls.append(("handle", wid))
        return wid


@pytest.fixture
def fake():
    c = FakeClient()
    tc.set_client(c)
    yield c
    tc.set_client(None)


async def test_update_task_starts_missing_workflow(fake):
    req = ActionRequest("u1", 3)
    assert await tc.update_task("ws1", "t1", "stop", req) == "ok"
    _, update, arg, op = fake.calls[0]
    assert (update, arg) == ("stop", req)
    assert op._start_workflow_input.id == "task:ws1:t1"
    assert op._start_workflow_input.args == [TaskStart("ws1", "t1", "migrated")]
    assert op._start_workflow_input.id_conflict_policy == WorkflowIDConflictPolicy.USE_EXISTING


async def test_signal_router_signal_with_start(fake):
    ref = TriggerRef("ws1", "chat:m1", "shared", "respond")
    await tc.signal_router(ref)
    _, workflow, _, kw = fake.calls[0]
    assert workflow == "RouterWorkflow"
    assert kw["id"] == "router:ws1"
    assert kw["start_signal"] == "trigger"
    assert kw["start_signal_args"] == [ref]
    assert kw["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


async def test_start_task_workflow_reuses_existing(fake):
    await tc.start_task_workflow("ws1", "t1", "router")
    _, workflow, arg, kw = fake.calls[0]
    assert (workflow, arg, kw["id"]) == ("TaskWorkflow", TaskStart("ws1", "t1", "router"), "task:ws1:t1")
    assert kw["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert kw["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE


async def test_signal_task_input_signal_with_start(fake):
    ref = TaskInputRef("chat:m2", "message")
    await tc.signal_task_input("ws1", "t1", ref)
    _, workflow, arg, kw = fake.calls[0]
    assert arg == TaskStart("ws1", "t1", "migrated")
    assert (kw["start_signal"], kw["start_signal_args"]) == ("input", [ref])


def test_task_handle(fake):
    assert tc.task_handle("ws1", "t1") == "task:ws1:t1"


async def test_helpers_raise_when_unconnected():
    tc.set_client(None)
    with pytest.raises(tc.TemporalUnavailable):
        await tc.signal_router(TriggerRef("ws1", "chat:m1", "shared", "respond"))
