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


async def test_pre_task_clarification_holds_first_worker_until_original_answer(temporal_env, fakes):
    """Meeting completion Ask uses update-with-start/migrated: zero Worker attempts before the card answer."""
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "migrated"), id=f"meeting-clarify:{uuid.uuid4()}", task_queue=queue
        )
        qid = await h.execute_update(TaskWorkflow.ask, AskRef("ask:meeting", "question", "question", True, True))
        await _until(lambda: "relay_question" in fakes.names())
        assert "begin_attempt" not in fakes.names() and "worker_turn" not in fakes.names()
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:yes", "message"))
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.answer, _press(qid=qid, option="answer", by=PRIYA))
        assert "begin_attempt" not in fakes.names()
        await h.execute_update(TaskWorkflow.answer, _press(qid=qid, option="answer", by=SAM))
        await _until(lambda: "begin_attempt" in fakes.names())
        assert fakes.args("record_answer")[0][2].by_user_id == SAM
        assert "answer:" + qid + ":c-1" in fakes.args("begin_attempt")[0][2]


async def test_immediate_answer_during_pre_task_question_relay_has_no_late_waiting_write(temporal_env, fakes):
    relay_entered, release_relay = asyncio.Event(), asyncio.Event()

    async def relay(*_):
        relay_entered.set()
        await release_relay.wait()

    fakes.behaviour["relay_question"] = relay
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "migrated"), id=f"meeting-race:{uuid.uuid4()}", task_queue=queue
        )
        ask = asyncio.create_task(
            h.execute_update(TaskWorkflow.ask, AskRef("ask:fast", "question", "question", True, True))
        )
        await asyncio.wait_for(relay_entered.wait(), timeout=5)
        assert fakes.args("transition") == [(WS, TID, "waiting", "question")]
        await h.execute_update(TaskWorkflow.answer, _press(qid="q-ask:fast", option="answer"))
        await _until(lambda: "begin_attempt" in fakes.names())
        worker_start = fakes.names().index("begin_attempt")
        release_relay.set()
        await ask
        assert not any(name == "transition" and args[2] == "waiting" for name, args in fakes.calls[worker_start:])


async def test_before_attempt_question_refuses_an_existing_worker_attempt(temporal_env, fakes):
    async with worker(temporal_env, fakes) as queue:
        h = await _start(temporal_env, queue)
        await _until(lambda: "relay_question" in fakes.names())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.ask, AskRef("ask:late", "question", "question", True, True))


async def test_concurrent_open_ask_misses_share_one_waiting_write_even_with_immediate_answer(
    temporal_env, fakes, monkeypatch
):
    from unittest.mock import AsyncMock
    from core.services import task_asks
    from core.temporal import client

    both_missed, release_reads, release_open, release_relay = (asyncio.Event() for _ in range(4))
    state = {"reads": 0, "opened": False}

    async def get_question(*_):
        if state["opened"]:
            return {"qid": "meeting-duplicate", "state": "open"}
        state["reads"] += 1
        if state["reads"] == 2:
            both_missed.set()
        await release_reads.wait()
        return None

    async def opened(*_):
        await release_open.wait()
        state["opened"] = True
        return QuestionRef("meeting-duplicate", [SAM], True)

    async def relay(*_):
        await release_relay.wait()

    monkeypatch.setattr(task_asks.questions_repo, "get_question", get_question)
    monkeypatch.setattr(task_asks.tasks_repo, "put_trigger", AsyncMock())
    fakes.behaviour["open_asked_question"] = opened
    fakes.behaviour["relay_question"] = relay
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "migrated"), id=f"meeting-duplicate:{uuid.uuid4()}", task_queue=queue
        )

        async def update(ws, task_id, name, ref):
            assert (ws, task_id, name) == (WS, TID, "ask")
            return await h.execute_update(TaskWorkflow.ask, ref)

        monkeypatch.setattr(client, "update_task", update)

        async def call():
            return await task_asks.open_ask(
                WS,
                {"taskId": TID},
                qid="meeting-duplicate",
                kind="question",
                subkind="question",
                title="Which report?",
                text="Weekly or monthly?",
                options=[],
                blocking=True,
                attempt=None,
                run_id=None,
                before_attempt=True,
            )

        first, second = asyncio.create_task(call()), asyncio.create_task(call())
        await asyncio.wait_for(both_missed.wait(), 5)
        release_reads.set()
        await _until(lambda: "open_asked_question" in fakes.names())
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(TaskWorkflow.ask, AskRef("ask:different", "question", "question", True, True))
        release_open.set()
        await _until(lambda: "relay_question" in fakes.names())
        await h.execute_update(TaskWorkflow.answer, _press(qid="meeting-duplicate", option="answer"))
        await _until(lambda: "begin_attempt" in fakes.names())
        worker_start = fakes.names().index("begin_attempt")
        release_relay.set()
        assert [r["qid"] for r in await asyncio.gather(first, second)] == ["meeting-duplicate"] * 2
        assert fakes.args("transition").count((WS, TID, "waiting", "question")) == 1
        assert len(fakes.args("open_asked_question")) == 1
        assert not any(name == "transition" and args[2] == "waiting" for name, args in fakes.calls[worker_start:])


@pytest.mark.parametrize("phase", ["transition", "open_asked_question"])
async def test_typed_input_cannot_start_worker_while_pre_task_question_is_opening(temporal_env, fakes, phase):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*_):
        entered.set()
        await release.wait()
        return QuestionRef("opening-question", [SAM], True) if phase == "open_asked_question" else None

    fakes.behaviour[phase] = blocked
    if phase != "open_asked_question":
        fakes.behaviour["open_asked_question"] = lambda *_: QuestionRef("opening-question", [SAM], True)
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "migrated"), id=f"meeting-opening:{uuid.uuid4()}", task_queue=queue
        )
        ask = asyncio.create_task(
            h.execute_update(TaskWorkflow.ask, AskRef("ask:opening", "question", "question", True, True))
        )
        await asyncio.wait_for(entered.wait(), 5)
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:yes", "message"))
        await asyncio.sleep(0.2)
        assert "begin_attempt" not in fakes.names() and "worker_turn" not in fakes.names()
        release.set()
        await ask
        assert "begin_attempt" not in fakes.names()
        await h.execute_update(TaskWorkflow.answer, _press(qid="opening-question", option="answer"))
        await _until(lambda: "begin_attempt" in fakes.names())


@pytest.mark.parametrize("reason", ["migrated", "router"])
async def test_persisted_initial_question_blocks_earliest_input_and_preserves_it_until_answer(
    temporal_env, fakes, reason
):
    from core.temporal.types import AttemptRef, InitialQuestionRef

    initial = InitialQuestionRef("initial-question", "ask:initial-question", SAM)
    calls = 0

    def begin(*_):
        nonlocal calls
        calls += 1
        return AttemptRef(0, "", initial) if calls == 1 else AttemptRef(1, str(uuid.uuid4()))

    fakes.behaviour["begin_attempt"] = begin
    fakes.behaviour["open_asked_question"] = lambda *_: QuestionRef(initial.qid, [SAM], True)
    fakes.turns = [WorkerTurnOut("completed")]
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, reason), id=f"row-visible:{uuid.uuid4()}", task_queue=queue
        )
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:before-open-ask", "message"))
        await _until(lambda: "relay_question" in fakes.names())
        assert "worker_turn" not in fakes.names()
        assert fakes.names().count("release_slot") == 1
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:plain-yes", "message"))
        await asyncio.sleep(0.2)
        assert fakes.names().count("begin_attempt") == 1
        await h.execute_update(TaskWorkflow.answer, _press(qid=initial.qid, option="answer"))
        await _until(lambda: "worker_turn" in fakes.names())
        inputs = fakes.args("begin_attempt")[1][2]
        assert "chat:before-open-ask" in inputs and "chat:plain-yes" in inputs
        assert inputs.count("answer:initial-question:c-1") == 1


async def test_cancelled_persisted_initial_question_stays_blocked_on_plain_input(temporal_env, fakes):
    from core.temporal.types import AttemptRef, InitialQuestionRef

    fakes.behaviour["begin_attempt"] = lambda *_: AttemptRef(
        0, "", InitialQuestionRef("cancelled", "ask:cancelled", SAM, "cancelled")
    )
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TID, "router"), id=f"initial-cancelled:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "transition" in fakes.names())
        await h.signal(TaskWorkflow.input, TaskInputRef("chat:yes", "message"))
        await asyncio.sleep(0.2)
        assert "worker_turn" not in fakes.names() and len(fakes.args("begin_attempt")) == 1


def test_old_attempt_result_payload_defaults_to_no_initial_question_gate():
    from temporalio.converter import value_to_type
    from core.temporal.types import AttemptRef

    assert value_to_type(AttemptRef, {"n": 1, "run_id": "recorded-before-gate"}) == AttemptRef(
        1, "recorded-before-gate"
    )


async def test_stop_during_initial_question_open_cancels_late_row_without_relay(temporal_env, fakes):
    entered, release = asyncio.Event(), asyncio.Event()

    async def opening(*_):
        entered.set()
        await release.wait()
        return QuestionRef("late-initial", [SAM], True)

    fakes.behaviour["open_asked_question"] = opening
    async with worker(temporal_env, fakes) as queue:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run,
            TaskStart(WS, TID, "migrated"),
            id=f"meeting-stop-opening:{uuid.uuid4()}",
            task_queue=queue,
        )
        ask = asyncio.create_task(
            h.execute_update(
                TaskWorkflow.ask,
                AskRef("ask:late-initial", "question", "question", True, True),
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        await h.execute_update(TaskWorkflow.stop, StopRequest(PRIYA))
        assert fakes.args("transition")[-1] == (WS, TID, "resolved", "stopped")
        release.set()
        assert await ask == "late-initial"
        await h.result()
    assert fakes.args("cancel_open_questions") == [
        (WS, TID, PRIYA, "stopped"),
        (WS, TID, PRIYA, "task_closed"),
    ]
    assert "relay_question" not in fakes.names()
    assert "worker_turn" not in fakes.names()
