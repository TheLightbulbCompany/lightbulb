"""Landing (wave2-Q Task 6): `apply_effect` over the moto table with fake executors, and the workflow's review
branch with fake Activities. Review focus 4: a Landing retried after the send succeeded sends once."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from core import dynamodb
from core.repositories import effects_repo, tasks_repo
from core.services import effect_executors as ex
from core.services import questions
from core.temporal import task_questions as tq
from core.temporal.types import TaskStart, WorkerTurnOut
from core.temporal.workflows import WORKFLOWS, TaskWorkflow

from .conftest import TASK, WS
from .test_task_questions_workflow import QFakes, _press, _until

SAM = "user_sam"
POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}
IDEM = f"{TASK}:1:c1"


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
    monkeypatch.setattr(
        questions.clerk_admin,
        "list_organization_members",
        AsyncMock(return_value=[{"user_id": SAM, "role": "org:member"}]),
    )
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


async def test_send_answer_asks_for_landing(world):
    qid = await _review_question()
    out = await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    assert out.land_idem_key == IDEM


async def test_request_changes_resumes_worker(world):
    qid = await _review_question()
    out = await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "request_changes", None, SAM, "c-1"))
    assert out.land_idem_key is None
    assert (await effects_repo.get_effect(WS, IDEM))["state"] == "denied"


async def test_unparsed_draft_resumes_worker(world):
    """A draft the classifier could not parse has no executor: the approval goes back to the Worker."""
    qid = await _review_question()
    item = await effects_repo.get_effect(WS, IDEM)
    dynamodb.get_table("tasks").update_item(
        Key={"pk": item["pk"], "sk": item["sk"]},
        UpdateExpression="SET executorOk = :f",
        ExpressionAttributeValues={":f": False},
    )
    out = await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    assert out.land_idem_key is None


async def test_intent_written_before_executor_then_done(world):
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == tq.LandResult("done")
    assert world["sends"] == [("intent", SAM)]
    effect = await effects_repo.get_effect(WS, IDEM)
    assert (effect["state"], effect["resultRef"]) == ("done", "mail:sent")


async def test_apply_effect_retry_sends_once(world, monkeypatch):
    """The first run sent, then died before recording done: the retry finds intent it did not write → unknown."""
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    real = effects_repo.mark_effect_done
    monkeypatch.setattr(effects_repo, "mark_effect_done", AsyncMock(side_effect=RuntimeError("ddb blip")))
    with pytest.raises(RuntimeError):
        await _run(tq.apply_effect, WS, TASK, IDEM, SAM)
    monkeypatch.setattr(effects_repo, "mark_effect_done", real)
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == tq.LandResult("unknown")
    assert len(world["sends"]) == 1
    assert (await effects_repo.get_effect(WS, IDEM))["state"] == "unknown"
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == tq.LandResult("unknown")  # still never re-sent
    assert len(world["sends"]) == 1


async def test_unreconcilable_outcome_unknown(world, monkeypatch):
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    monkeypatch.setitem(ex.EXECUTORS, "mail.send", AsyncMock(side_effect=ex.ExecutorUnknown("ses")))
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == tq.LandResult("unknown")
    assert (await effects_repo.get_effect(WS, IDEM))["state"] == "unknown"


async def test_refusal_goes_back_to_the_worker(world, monkeypatch):
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    monkeypatch.setitem(ex.EXECUTORS, "mail.send", AsyncMock(side_effect=ex.ExecutorRefused("rate_limited")))
    res = await _run(tq.apply_effect, WS, TASK, IDEM, SAM)
    assert res.outcome == "refused" and res.trigger_id
    assert (await tasks_repo.get_trigger(WS, res.trigger_id))["ref"]["refused"] == "rate_limited"
    effect = await effects_repo.get_effect(WS, IDEM)
    assert (effect["state"], effect["resultRef"]) == ("done", "refused:rate_limited")
    # Not unknown: the same draft can be asked again (gate sees done → a fresh approval).
    assert questions.gate({**effect, "idemKey": f"{TASK}:2:c9"}, [effect]).decision == "needs_approval"


async def test_landing_field_set_and_cleared(world, monkeypatch):
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    seen = []

    async def mail(owner, operands, by):
        seen.append((await tasks_repo.get_task(WS, TASK)).get("landingEffect"))
        return "mail:sent"

    monkeypatch.setitem(ex.EXECUTORS, "mail.send", mail)
    await _run(tq.apply_effect, WS, TASK, IDEM, SAM)
    assert seen == [IDEM]
    assert (await tasks_repo.get_task(WS, TASK)).get("landingEffect") is None


async def test_worker_used_the_approval_first(world):
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    retry = {**(await effects_repo.get_effect(WS, IDEM)), "idemKey": f"{TASK}:2:c7", "attempt": 2}
    for k in ("pk", "sk", "type", "version", "createdAt", "updatedAt", "state", "questionId"):
        retry.pop(k, None)
    assert await effects_repo.use_approval(WS, IDEM, "h1", new_effect=retry)
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == tq.LandResult("used")
    assert world["sends"] == []


async def test_crash_after_refused_done_reports_refused_on_retry(world):
    """The first run recorded the refusal, then died before its trigger: the retry finishes it, never "landed"."""
    qid = await _review_question()
    await _run(tq.record_answer, WS, TASK, tq.AnswerRef(qid, "send", None, SAM, "c-1"))
    assert await effects_repo.use_approval(WS, IDEM, "h1")
    await effects_repo.mark_effect_done(WS, IDEM, disposition="done", result_ref="refused:rate_limited")
    res = await _run(tq.apply_effect, WS, TASK, IDEM, SAM)
    assert res.outcome == "refused" and res.trigger_id
    assert (await tasks_repo.get_trigger(WS, res.trigger_id))["ref"]["refused"] == "rate_limited"
    assert await _run(tq.apply_effect, WS, TASK, IDEM, SAM) == res  # and again: the same trigger, once


async def test_review_kind_without_executor_resumes_worker(world):
    """text.send is a review kind with no backend executor: the answer goes back to the Worker."""
    await effects_repo.put_effect(
        WS,
        {
            "idemKey": f"{TASK}:1:t1",
            "taskId": TASK,
            "attempt": 1,
            "kind": "text.send",
            "tool": "terminal",
            "operands": {"agentId": "a-1", "replyKey": "K1", "body": "On my way"},
            "operandsHash": "h-t",
            "state": "proposed",
            "executorOk": True,
        },
    )
    [ref] = await ActivityEnvironment().run(tq.open_questions, WS, TASK)
    out = await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, "send", None, SAM, "c-1"))
    assert out.land_idem_key is None and out.trigger_id == f"answer:{ref.qid}:c-1"
    # A stray Landing call on it: nothing consumed, the Worker gets the answer as input.
    res = await _run(tq.apply_effect, WS, TASK, f"{TASK}:1:t1", SAM)
    assert res == tq.LandResult("refused", f"answer:{ref.qid}:c-1")
    assert (await effects_repo.get_effect(WS, f"{TASK}:1:t1"))["state"] == "approved"
    assert world["sends"] == []


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


async def test_review_send_lands_and_goes_idle(temporal_env):
    fakes = LFakes("done")
    worker, queue = await _wf(temporal_env, fakes)
    async with worker:
        h = await temporal_env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
        )
        await _until(lambda: "relay_question" in fakes.names())
        result = await h.execute_update(TaskWorkflow.answer, _press("send"))
        assert result.landing is True
        await _until(lambda: (WS, TASK, "idle", "landed") in fakes.args("transition"))
        await asyncio.sleep(0.3)
    assert fakes.args("apply_effect") == [(WS, TASK, IDEM, SAM)]
    assert fakes.names().count("begin_attempt") == 1  # landed by the backend, not by another Worker turn


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
