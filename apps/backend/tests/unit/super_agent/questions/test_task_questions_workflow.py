"""Questions on TaskWorkflow with fake Activities on the time-skipping server (wave2-Q Task 3).

Adversarial: a non-answerer is refused before any Activity, typed input never answers and waits behind the open
question, an answer after Stop is refused, a replayed press doesn't start a second attempt."""

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from core.temporal.task_questions import AnswerOut, AnswerRef, QuestionRef
from core.temporal.types import StopRequest, TaskInputRef, TaskStart, WorkerTurnOut
from core.temporal.workflows import WORKFLOWS, TaskWorkflow

from ..orchestration.fake_activities import Fakes

WS, TID = "org_1", "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
SAM, PRIYA = "user_sam", "user_priya"


class QFakes(Fakes):
    def __init__(self) -> None:
        super().__init__()
        self.turns = [WorkerTurnOut("waiting_approval"), WorkerTurnOut("completed")]
        self.behaviour["worker_turn"] = lambda inp: self.turns.pop(0) if self.turns else WorkerTurnOut("completed")
        self.behaviour["open_questions"] = lambda ws, tid: [QuestionRef("q-1", [SAM])]
        self.behaviour["record_answer"] = lambda ws, tid, ref: AnswerOut(
            f"answer:{ref.qid}:{ref.client_answer_id}", ref.option_id
        )

    def activities(self) -> list:
        f = self

        @activity.defn(name="open_questions")
        async def open_questions(ws: str, task_id: str) -> list[QuestionRef]:
            return await f._call("open_questions", ws, task_id, default=[])

        @activity.defn(name="relay_question")
        async def relay_question(ws: str, task_id: str, qid: str) -> None:
            await f._call("relay_question", ws, task_id, qid)

        @activity.defn(name="remind_question")
        async def remind_question(ws: str, task_id: str, qid: str) -> None:
            await f._call("remind_question", ws, task_id, qid)

        @activity.defn(name="record_answer")
        async def record_answer(ws: str, task_id: str, ref: AnswerRef) -> AnswerOut:
            return await f._call("record_answer", ws, task_id, ref)

        @activity.defn(name="cancel_open_questions")
        async def cancel_open_questions(ws: str, task_id: str, by: str, reason: str) -> list[str]:
            return await f._call("cancel_open_questions", ws, task_id, by, reason, default=[])

        return [
            *super().activities(),
            open_questions,
            relay_question,
            remind_question,
            record_answer,
            cancel_open_questions,
        ]


@pytest.fixture
def fakes():
    return QFakes()


@contextlib.asynccontextmanager
async def worker(env, fakes):
    queue = f"q-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        yield queue


async def _start(env, queue):
    return await env.client.start_workflow(
        TaskWorkflow.run, TaskStart(WS, TID, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
    )


async def _until(pred, n=200):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def _press(option="allow_once", by=SAM, cid="c-1", qid="q-1"):
    return AnswerRef(qid, option, None, by, cid)


async def test_waiting_approval_opens_question_and_waits(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await asyncio.sleep(0.3)
    assert fakes.names()[-3:] == ["end_attempt", "open_questions", "relay_question"]
    assert fakes.args("end_attempt")[0][3] == "waiting_approval"
    assert fakes.names().count("begin_attempt") == 1  # no further attempt while the question is open


async def test_answer_allow_resumes_worker_with_input(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        result = await h.execute_update(TaskWorkflow.answer, _press())
        assert result.state == "answered"
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert fakes.args("begin_attempt")[1][2] == ["answer:q-1:c-1"]


async def test_answer_deny_resumes_with_denied_input(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press("deny"))
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert fakes.args("record_answer")[0][2].option_id == "deny"
    assert fakes.args("begin_attempt")[1][2] == ["answer:q-1:c-1"]


async def test_answer_validator_rejects_non_answerer(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press(by=PRIYA))
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press(qid="q-unknown"))
    assert "record_answer" not in fakes.names()


async def test_typed_go_ahead_waits_and_never_answers(temporal_env, fakes):
    """Priya's "go ahead" reaches the task as input; the question stays open and no attempt starts."""
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:go-ahead", "message"))
        await asyncio.sleep(0.5)
        assert fakes.names().count("begin_attempt") == 1 and "record_answer" not in fakes.names()
        await h.execute_update(TaskWorkflow.answer, _press())
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert sorted(fakes.args("begin_attempt")[1][2]) == ["answer:q-1:c-1", "chat:go-ahead"]


async def test_replayed_press_does_not_answer_twice(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press())  # closed now; the route answers 200 from the row
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
    assert fakes.names().count("record_answer") == 1


async def test_stop_cancels_open_questions(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.stop, StopRequest(PRIYA))
        await h.result()
        assert fakes.args("cancel_open_questions") == [(WS, TID, PRIYA, "stopped")]
        assert fakes.args("transition")[-1] == (WS, TID, "resolved", "stopped")
        assert "record_answer" not in fakes.names()


async def test_answer_after_stop_refused(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())

        async def slow_cancel(*_):
            await asyncio.sleep(0.5)  # keep the workflow open while Sam presses

        fakes.behaviour["cancel_open_questions"] = slow_cancel
        stop = asyncio.create_task(h.execute_update(TaskWorkflow.stop, StopRequest(PRIYA)))
        await _until(lambda: "cancel_open_questions" in fakes.names())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press())
        await stop
    assert "record_answer" not in fakes.names() and fakes.names().count("begin_attempt") == 1


async def test_no_answerer_resolves_instead_of_waiting(temporal_env, fakes):
    def nobody(*_):
        raise ApplicationError("no admins", type="NoAnswerer", non_retryable=True)

    fakes.behaviour["open_questions"] = nobody
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await h.result()
    assert fakes.args("transition")[-1] == (WS, TID, "resolved", "no_answerer")
    assert "relay_question" not in fakes.names()


async def test_reminder_once_after_24h(temporal_env, fakes):
    async with worker(temporal_env, fakes) as q:
        await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await temporal_env.sleep(timedelta(hours=25))
        await _until(lambda: "remind_question" in fakes.names())
        await temporal_env.sleep(timedelta(hours=48))
        await asyncio.sleep(0.3)
    assert fakes.args("remind_question") == [(WS, TID, "q-1")]
