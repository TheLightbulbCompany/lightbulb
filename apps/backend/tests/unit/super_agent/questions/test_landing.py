"""Landing (wave2-Q Task 6): `apply_effect` over the moto table with fake executors, and the workflow's review
branch with fake Activities. Review focus 4: a Landing retried after the send succeeded sends once."""

import uuid
from unittest.mock import AsyncMock

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from core.repositories import effects_repo
from core.services import effect_executors as ex
from core.services import questions
from core.temporal import task_questions as tq
from core.temporal.types import TaskStart, WorkerTurnOut
from core.temporal.workflows import WORKFLOWS, TaskWorkflow

from .conftest import TASK, WS
from .test_task_questions_workflow import QFakes, _press, _until

SAM = "org_1"
POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}
IDEM = f"{TASK}:1:c1"


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
    sends = []

    async def mail(owner, operands, by):
        effect = await effects_repo.get_effect(WS, IDEM)
        sends.append((effect["state"], by))  # intent is written before the executor runs
        return "mail:sent"

    monkeypatch.setitem(ex.EXECUTORS, "mail.send", mail)
    return {"sends": sends}


async def _review_question():
    await effects_repo.put_effect(
        WS,
        {
            "idemKey": IDEM,
            "taskId": TASK,
            "attempt": 1,
            "kind": "mail.send",
            "tool": "terminal",
            "operands": {"agentId": "a-1", "to": "acme@example.com", "subject": "Q3", "body": "hi"},
            "operandsHash": "h1",
            "state": "proposed",
            "executorOk": True,
        },
    )
    [ref] = await ActivityEnvironment().run(tq.open_questions, WS, TASK)
    return ref.qid


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


def test_executor_kinds_pinned():
    assert tq.EXECUTOR_KINDS == set(ex.EXECUTORS)


# --- workflow ----------------------------------------------------------------------------------------


class LFakes(QFakes):
    def __init__(self, land="done"):
        super().__init__()
        self.behaviour["record_answer"] = lambda ws, tid, ref: tq.AnswerOut(
            f"answer:{ref.qid}:{ref.client_answer_id}", ref.option_id, IDEM if ref.option_id == "send" else None
        )
        self.behaviour["apply_effect"] = lambda *a: (
            tq.LandResult(land) if land != "refused" else tq.LandResult("refused", "answer:q-1:landing")
        )

    def activities(self):
        f = self

        @activity.defn(name="apply_effect")
        async def apply_effect(ws: str, task_id: str, idem: str, by: str) -> tq.LandResult:
            return await f._call("apply_effect", ws, task_id, idem, by)

        return [*super().activities(), apply_effect]


async def _wf(env, fakes):
    queue = f"q-{uuid.uuid4()}"
    worker = Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities())
    return worker, queue


async def test_landing_unknown_goes_idle_with_reason(temporal_env):
    fakes = LFakes("unknown")
    worker, queue = await _wf(temporal_env, fakes)
    async with worker:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press("send"))
        await _until(lambda: (WS, TASK, "idle", "effect_unknown") in fakes.args("transition"))


async def test_landing_refused_resumes_the_worker(temporal_env):
    fakes = LFakes("refused")
    fakes.turns = [WorkerTurnOut("waiting_approval"), WorkerTurnOut("completed")]
    worker, queue = await _wf(temporal_env, fakes)
    async with worker:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press("send"))
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert fakes.args("begin_attempt")[1][2] == ["answer:q-1:landing"]


async def test_request_changes_resumes_worker_without_landing(temporal_env):
    fakes = LFakes("done")
    worker, queue = await _wf(temporal_env, fakes)
    async with worker:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press("request_changes"))
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert "apply_effect" not in fakes.names()


async def test_landing_activity_failure_goes_idle_unknown(temporal_env):
    from temporalio.exceptions import ApplicationError

    fakes = LFakes("done")

    def broken(*_):
        raise ApplicationError("ddb gone", type="RuntimeError", non_retryable=True)

    fakes.behaviour["apply_effect"] = broken
    worker, queue = await _wf(temporal_env, fakes)
    async with worker:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press("send"))
        await _until(lambda: (WS, TASK, "idle", "effect_unknown") in fakes.args("transition"))
    assert fakes.names().count("begin_attempt") == 1
