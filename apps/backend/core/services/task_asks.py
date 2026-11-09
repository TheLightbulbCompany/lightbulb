"""A Question opened on a running task by something the Worker asked for: the Worker's own `request_approval` /
`ask_person` (runtime_internal's ask route) and a provisioning consent (spec 05 P1). One path: the ask text rides on
`TRIG#ask:{qid}`, the `ask` update on TaskWorkflow opens the row (answerers per spec 05 §3.3) and relays it."""

from __future__ import annotations

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import questions_repo, tasks_repo


class AskRefused(Exception):
    """`code`: temporal_unavailable (retry) | task_closed | not_opened | question_not_opened."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _record_ask(
    ws: str,
    task: dict,
    *,
    qid: str,
    kind: str,
    subkind: str,
    title: str,
    text: str,
    options: list[dict],
    blocking: bool,
    attempt: int | None,
    run_id: str | None,
    preview: dict | None = None,
    before_attempt: bool = False,
) -> str:
    trigger_id = ts.trigger_id("ask", qid)
    await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": "ask",
            "ref": {
                "taskId": task["taskId"],
                "qid": qid,
                "kind": kind,
                "subkind": subkind,
                "title": title,
                "text": text,
                "options": options,
                "blocking": blocking,
                "attempt": attempt,
                "runId": run_id,
                **({"initialPrincipalUserId": task.get("ownerUserId")} if before_attempt else {}),
                **({"preview": preview} if preview else {}),
            },
            "admission": "respond",
            "lane": task.get("lane") or "shared",
            "receivedAt": utc_now_iso(),
        },
    )
    return trigger_id


async def open_ask(
    ws: str,
    task: dict,
    *,
    qid: str,
    kind: str,
    subkind: str,
    title: str,
    text: str,
    options: list[dict],
    blocking: bool,
    attempt: int | None,
    run_id: str | None,
    preview: dict | None = None,
    before_attempt: bool = False,
) -> dict:
    """The Question row (an existing one replays). Raises `AskRefused`."""
    from temporalio.service import RPCError

    from core.temporal import client as temporal
    from core.temporal.task_questions import AskRef

    existing = await questions_repo.get_question(ws, task["taskId"], qid)
    if existing is not None:
        return existing
    trigger_id = await _record_ask(
        ws,
        task,
        qid=qid,
        kind=kind,
        subkind=subkind,
        title=title,
        text=text,
        options=options,
        blocking=blocking,
        attempt=attempt,
        run_id=run_id,
        preview=preview,
        before_attempt=before_attempt,
    )
    try:
        await temporal.update_task(
            ws, task["taskId"], "ask", AskRef(trigger_id, kind, subkind, blocking, before_attempt)
        )
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise AskRefused("temporal_unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- the update was refused (stopped task, nobody can answer)
        from routers.questions import _failure_codes  # walks the update failure's cause chain

        raise AskRefused("task_closed" if "task_closed" in _failure_codes(exc) else "not_opened") from exc
    opened = await questions_repo.get_question(ws, task["taskId"], qid)
    if opened is None:
        raise AskRefused("question_not_opened")
    return opened


def _initial_binding(task: dict) -> ts.InitialQuestion:
    from core.services import questions

    ref = ts.InitialQuestion.model_validate(task["initialQuestion"])
    if (
        ref.qid != questions.qid_for("meeting-clarification:" + task["taskId"])
        or ref.trigger_id != ts.trigger_id("ask", ref.qid)
        or ref.principal_user_id != task.get("ownerUserId")
        or task.get("origin") != "meeting"
    ):
        raise AskRefused("invalid_initial_question_binding")
    return ref


async def prepare_initial_question(ws: str, task_id: str, principal: str, lane: str, question: dict) -> dict:
    """Store its bounded payload first; the immutable reference is written WITH the Task row."""
    from core.services import questions

    if (
        not principal
        or question.get("qid") != questions.qid_for("meeting-clarification:" + task_id)
        or question.get("kind") != "question"
        or question.get("subkind") != "question"
        or not question.get("blocking")
        or not question.get("before_attempt")
        or question.get("attempt") is not None
        or question.get("run_id") is not None
    ):
        raise AskRefused("invalid_initial_question")
    trigger_id = await _record_ask(ws, {"taskId": task_id, "ownerUserId": principal, "lane": lane}, **question)
    return {"qid": question["qid"], "triggerId": trigger_id, "principalUserId": principal}


async def initial_question(ws: str, task: dict):
    """Questions-owned gate on the existing begin_attempt read; no attempt may precede it."""
    from core.services import meeting_context, questions
    from core.temporal.task_questions import AnswerRef, record_answer
    from core.temporal.types import InitialQuestionRef

    if not task.get("initialQuestion"):
        return None
    ref = _initial_binding(task)
    original = await tasks_repo.get_trigger(ws, task["triggerId"])
    if not original or original.get("actorUserId") != ref.principal_user_id:
        raise AskRefused("invalid_initial_principal")
    await meeting_context.authorize(ws, original)
    payload = await tasks_repo.get_trigger(ws, ref.trigger_id) or {}
    ask = payload.get("ref") or {}
    if (
        payload.get("source") != "ask"
        or ask.get("taskId") != task["taskId"]
        or ask.get("qid") != ref.qid
        or ask.get("initialPrincipalUserId") != ref.principal_user_id
        or ask.get("kind") != "question"
        or ask.get("subkind") != "question"
        or ask.get("blocking") is not True
    ):
        raise AskRefused("invalid_initial_question_payload")
    q = await questions_repo.get_question(ws, task["taskId"], ref.qid)
    result = InitialQuestionRef(ref.qid, ref.trigger_id, ref.principal_user_id)
    if q and (
        q.get("taskId") != task["taskId"]
        or q.get("qid") != ref.qid
        or q.get("kind") != "question"
        or q.get("subkind") != "question"
        or q.get("effectIdemKey")
    ):
        raise AskRefused("invalid_initial_question_row")
    if not q or q.get("state") == "open":
        return result
    if q.get("state") != "answered":
        return InitialQuestionRef(ref.qid, ref.trigger_id, ref.principal_user_id, "cancelled")
    if (
        q.get("taskId") != task["taskId"]
        or q.get("kind") != "question"
        or q.get("answeredBy") != ref.principal_user_id
        or not q.get("clientAnswerId")
        or not await questions.can_answer_now(ws, q, ref.principal_user_id)
    ):
        raise AskRefused("invalid_initial_answer")
    answer = q.get("answer") or {}
    # Reuse the idempotent authenticated-answer owner to recover the canonical trigger after a crash.
    out = await record_answer(
        ws,
        task["taskId"],
        AnswerRef(ref.qid, answer["optionId"], answer.get("triggerId"), ref.principal_user_id, q["clientAnswerId"]),
    )
    canonical = await tasks_repo.get_trigger(ws, out.trigger_id) or {}
    if (
        canonical.get("source") != "answer"
        or canonical.get("actorUserId") != ref.principal_user_id
        or (canonical.get("ref") or {}).get("taskId") != task["taskId"]
        or (canonical.get("ref") or {}).get("qid") != ref.qid
    ):
        raise AskRefused("invalid_initial_answer_trigger")
    return InitialQuestionRef(ref.qid, ref.trigger_id, ref.principal_user_id, "answered", out.trigger_id)
