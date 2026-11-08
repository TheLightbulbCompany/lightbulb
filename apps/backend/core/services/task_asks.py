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
) -> dict:
    """The Question row (an existing one replays). Raises `AskRefused`."""
    from temporalio.service import RPCError

    from core.temporal import client as temporal
    from core.temporal.task_questions import AskRef

    existing = await questions_repo.get_question(ws, task["taskId"], qid)
    if existing is not None:
        return existing
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
                **({"preview": preview} if preview else {}),
            },
            "admission": "respond",
            "lane": task.get("lane") or "shared",
            "receivedAt": utc_now_iso(),
        },
    )
    try:
        await temporal.update_task(ws, task["taskId"], "ask", AskRef(trigger_id, kind, subkind, blocking))
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise AskRefused("temporal_unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- the update was refused (stopped task, nobody can answer)
        from routers.questions import _failure_codes  # walks the update failure's cause chain

        raise AskRefused("task_closed" if "task_closed" in _failure_codes(exc) else "not_opened") from exc
    opened = await questions_repo.get_question(ws, task["taskId"], qid)
    if opened is None:
        raise AskRefused("question_not_opened")
    return opened
