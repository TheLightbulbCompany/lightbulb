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
from temporalio.service import RPCError
from temporalio.worker import Worker

from core.temporal.task_questions import AnswerOut, AnswerRef, AskRef, QuestionRef
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

        @activity.defn(name="note_answerers_unavailable")
        async def note_answerers_unavailable(ws: str, task_id: str) -> None:
            await f._call("note_answerers_unavailable", ws, task_id)

        @activity.defn(name="cancel_open_questions")
        async def cancel_open_questions(ws: str, task_id: str, by: str, reason: str) -> list[str]:
            return await f._call("cancel_open_questions", ws, task_id, by, reason, default=[])

        @activity.defn(name="open_asked_question")
        async def open_asked_question(ws: str, task_id: str, ref: AskRef) -> QuestionRef:
            return await f._call(
                "open_asked_question", ws, task_id, ref, default=QuestionRef(f"q-{ref.trigger_id}", [SAM], ref.blocking)
            )

        return [
            *super().activities(),
            open_questions,
            relay_question,
            remind_question,
            record_answer,
            note_answerers_unavailable,
            cancel_open_questions,
            open_asked_question,
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


async def test_resolve_cancels_open_questions(temporal_env, fakes):
    """Audit #5: a resolve (not only Stop) leaves no open Question behind, cancelled under the resolver's id before
    the transition. Its history is histories/O-cancel-questions-on-end-v1.json (test_replay)."""
    from core.temporal.types import ActionRequest

    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.resolve, ActionRequest(SAM))
        await h.result()
        assert fakes.args("cancel_open_questions") == [(WS, TID, SAM, "resolved")]
        assert fakes.names().index("cancel_open_questions") < fakes.names().index("transition")
        assert fakes.args("transition")[-1] == (WS, TID, "resolved", "resolved")


async def test_a_failed_cancel_still_ends_the_task(temporal_env, fakes):
    """The cancel Activity failing after its retries never keeps the task from resolving."""
    from core.temporal.types import ActionRequest

    from ..orchestration.fake_activities import non_retryable

    fakes.behaviour["cancel_open_questions"] = non_retryable
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.resolve, ActionRequest(SAM))
        await h.result()
        assert fakes.args("transition")[-1] == (WS, TID, "resolved", "resolved")


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


async def test_answer_after_close_refused(temporal_env, fakes):
    """N-F1: once Close (resolve) is queued, an "Allow once" is refused; nothing is recorded, no attempt runs."""
    from core.temporal.types import ActionRequest

    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())

        async def slow_cancel(*_):
            await asyncio.sleep(0.5)  # keep the workflow open while Sam presses

        fakes.behaviour["cancel_open_questions"] = slow_cancel
        close = asyncio.create_task(h.execute_update(TaskWorkflow.resolve, ActionRequest(PRIYA)))
        await _until(lambda: "cancel_open_questions" in fakes.names())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press())
        await close
        await h.result()
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


async def test_open_question_holds_the_task_past_the_quiet_resolve(temporal_env, fakes):
    """A task waiting on a person is never auto-resolved as quiet (7 days); it waits for the answer."""
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "relay_question" in fakes.names())
        await temporal_env.sleep(timedelta(days=9))
        await asyncio.sleep(0.3)
        assert ("resolved", "quiet") not in [(a[2], a[3]) for a in fakes.args("transition")]
        assert fakes.args("remind_question") == [(WS, TID, "q-1")]
        await h.execute_update(TaskWorkflow.answer, _press())
        await _until(lambda: fakes.names().count("begin_attempt") == 2)


async def test_answerer_lookup_error_leaves_the_task_waiting(temporal_env, fakes):
    """Clerk down after the retries: a line in the task room, no failure, and a reply re-runs the Worker."""
    calls = {"n": 0}

    def flaky(*_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApplicationError("clerk down", type="HTTPStatusError", non_retryable=True)
        return [QuestionRef("q-1", [SAM])]

    fakes.turns = [WorkerTurnOut("waiting_approval"), WorkerTurnOut("waiting_approval")]
    fakes.behaviour["open_questions"] = flaky
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "note_answerers_unavailable" in fakes.names())
        assert "transition" not in fakes.names() and "relay_question" not in fakes.names()
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:try-again", "message"))
        await _until(lambda: "relay_question" in fakes.names())
    assert fakes.names().count("begin_attempt") == 2


async def test_worker_ask_non_blocking_does_not_hold_the_next_attempt(temporal_env, fakes):
    """A Worker's question without `blocked`: relayed, open, and a new input still starts the next attempt."""
    fakes.turns = [WorkerTurnOut("completed"), WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        qid = await h.execute_update(TaskWorkflow.ask, AskRef("ask:1", "question", "question", False))
        assert qid == "q-ask:1"
        await _until(lambda: "relay_question" in fakes.names())
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:more", "message"))
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
        await h.execute_update(TaskWorkflow.ask, AskRef("ask:2", "question", "question", False))
        with pytest.raises(WorkflowUpdateFailedError):  # superseded in the workflow's books too
            await h.execute_update(TaskWorkflow.answer, _press(qid="q-ask:1", option="answer"))
        await h.execute_update(TaskWorkflow.answer, _press(qid="q-ask:2", option="answer"))


async def test_worker_approval_holds_until_answered(temporal_env, fakes):
    fakes.turns = [WorkerTurnOut("completed"), WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update(TaskWorkflow.ask, AskRef("ask:a", "approval", "purchase", True))
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:go", "message"))
        await asyncio.sleep(0.5)
        assert fakes.names().count("begin_attempt") == 1  # held behind the approval
        await h.execute_update(TaskWorkflow.answer, _press(qid="q-ask:a"))
        await _until(lambda: fakes.names().count("begin_attempt") == 2)


async def test_worker_ask_after_stop_refused(temporal_env, fakes):
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as q:
        h = await _start(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update(TaskWorkflow.stop, StopRequest(PRIYA))
        # Refused by the validator while stopping, or not found once the stopped workflow has completed.
        with pytest.raises((WorkflowUpdateFailedError, RPCError)):
            await h.execute_update(TaskWorkflow.ask, AskRef("ask:late", "question", "question", True))
    assert "open_asked_question" not in fakes.names()
