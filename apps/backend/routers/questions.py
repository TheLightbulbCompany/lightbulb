"""Super-agent Questions for people (spec 05 §5.1; contracts-v2 §3, §5, §14.2).

`GET /api/v1/needs-you` (the viewer's open Questions, from `NEEDS#{me}#`) and `GET /api/v1/tasks/{taskId}/questions`
(a task's history, answered and cancelled included; visible through `tasks_access.can_view`). Mounted before `routers/tasks.py` so `/tasks/{id}/questions` is never read as a task route.

`POST /api/v1/tasks/{taskId}/questions/{qid}/answer` is the ONLY way a Question is answered (spec 05 §3.5 layer 1):
a person's first-party Clerk session only (no computer, hook, hosted-MCP/OAuth or A2A caller), authority re-checked
now (`questions.can_answer_now`), then the `answer` update on `TaskWorkflow`. Typed text never answers anything.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from core import tasks_schema as ts
from core.constants import SYSTEM_ACTOR_ID
from core.auth import AuthContext, get_current_user, get_human_session, resolve_owner_id, security_optional
from core.dynamodb import utc_now_iso
from core.repositories import effects_repo, questions_repo, tasks_repo
from core.services import questions, tasks_access
from core.services.messaging import human_display_name
from core.temporal import client as temporal_client
from core.temporal.task_questions import AnswerRef

router = APIRouter()

_STYLES = {"deny": "danger", "request_changes": "secondary"}


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "not_found"})


async def _workspace(auth: AuthContext) -> str:
    ws = resolve_owner_id(auth)
    return ws


async def _people(ids: list[str | None]) -> dict[str, str]:
    unique = [u for u in dict.fromkeys(ids) if u]
    return dict(zip(unique, await asyncio.gather(*(human_display_name(u) for u in unique))))


def _view(q: dict, viewer: str, names: dict[str, str]) -> dict[str, Any]:
    """`QuestionView` (spec 05 §5.1, states from contracts-v2 §3). `canAnswer` is the stored list;
    the answer route re-checks authority (`questions.can_answer_now`)."""
    answerers = list(q.get("answerers") or [])
    is_open = q["state"] == "open"
    if not is_open:
        waiting = None
    elif viewer in answerers:
        waiting = "Waiting on you"
    else:
        waiting = questions.waiting_label([names.get(u, u) for u in answerers])

    def person(uid: str | None) -> dict | None:
        return {"id": uid, "name": names.get(uid, uid)} if uid else None

    return {
        "id": q["qid"],
        "taskId": q["taskId"],
        "kind": q["kind"],
        "subkind": q.get("subkind"),
        "title": q["title"],
        "text": q.get("text", ""),
        "preview": q.get("preview"),
        "options": [
            {"id": o["id"], "label": o["label"], "style": _STYLES.get(o["id"], "primary" if i == 0 else "secondary")}
            for i, o in enumerate(q.get("options") or [])
        ],
        "state": q["state"],
        "askedBy": q.get("askedBy"),
        "answerers": [person(u) for u in answerers],
        "canAnswer": is_open and viewer in answerers,
        "waitingLabel": waiting,
        "answer": q.get("answer"),
        "answeredBy": person(q.get("answeredBy")),
        "answeredAt": q.get("answeredAt"),
        "reason": q.get("reason"),
        "cancelledBy": None if q.get("cancelledBy") in _NOT_A_PERSON else person(q.get("cancelledBy")),
        "cancelledAt": q.get("cancelledAt"),
        "govRef": q.get("govRef"),
        "createdAt": q["createdAt"],
        "version": q["version"],
    }


async def _views(qs: list[dict], viewer: str) -> list[dict]:
    names = await _people(
        [u for q in qs for u in (*(q.get("answerers") or []), q.get("answeredBy"), q.get("cancelledBy"))]
    )
    return [_view(q, viewer, names) for q in qs]


@router.get("/needs-you")
async def needs_you(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    ws = await _workspace(auth)
    needs, nxt = await questions_repo.needs_for_user(ws, auth.user_id, cursor=cursor)
    found = await asyncio.gather(*(questions_repo.get_question(ws, n["taskId"], n["qid"]) for n in needs))
    open_qs = [q for q in found if q and q["state"] == "open" and auth.user_id in (q.get("answerers") or [])]
    return {"items": await _views(open_qs, auth.user_id), "next": nxt}


@router.get("/tasks/{task_id}/questions")
async def task_questions(task_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    ws = await _workspace(auth)
    task = await tasks_repo.get_task(ws, task_id)
    if task is None or not await tasks_access.can_view(task, auth.user_id):
        raise _not_found()  # not visible reads the same as missing
    qs = sorted(await questions_repo.list_for_task(ws, task_id), key=lambda q: q["createdAt"])
    return {"items": await _views(qs, auth.user_id)}


# --- answer -------------------------------------------------------------------------------------------


class AnswerBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    option_id: str = Field(min_length=1, max_length=64)
    text: str | None = Field(default=None, max_length=4000)
    edits: dict | None = None  # accepted for the spec 05 shape; no subkind takes edits in wave 2
    client_answer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expect_version: int | None = None


def _problem(status: int, code: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, **extra})


async def person_only(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security_optional)
) -> AuthContext:
    """A person pressing a button: a computer's proof header is refused before any token is read; then only a
    first-party Clerk session token (`get_human_session` refuses OAuth/hosted-MCP and template tokens; a hook
    token is not a Clerk JWT and fails as 401)."""
    if request.headers.get("X-Lightbulb-Proof"):
        raise _problem(403, "computer_caller")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_human_session(credentials)


def _failure_codes(err: BaseException) -> set[str]:
    """Every error type and message along a failed update's cause chain (validator message, Activity type)."""
    codes: set[str] = set()
    seen: BaseException | None = err
    while seen is not None:
        codes.add(type(seen).__name__)
        codes.add(str(getattr(seen, "message", "") or ""))
        if getattr(seen, "type", None):
            codes.add(seen.type)
        seen = seen.__cause__ or getattr(seen, "cause", None)
    return codes


async def _answered_view(ws: str, task: dict, qid: str, viewer: str) -> dict[str, Any]:
    tree = await tasks_repo.get_task_tree(ws, task["taskId"])
    tree["effects"] = await effects_repo.effects_of_task(ws, task["taskId"])
    q = next(q for q in tree["questions"] if q["qid"] == qid)
    [view] = await _views([q], viewer)
    return {"question": view, "next": tasks_access.summarize(tree, viewer)}


_CLOSED = frozenset({"resolved", "failed"})
_NOT_A_PERSON = frozenset({SYSTEM_ACTOR_ID, "watch"})  # the task ended it (resolve, failure, watchdog, watch)


def _not_open(q: dict) -> HTTPException:
    """409 with who closed it. Cancelled: `reason` (stopped, resolved, quiet, lost_run, watch_gave_up, ...) for the
    client's copy, and `by` only when a person did it ("This thread ended", never a system id as a name)."""
    if q["state"] == "cancelled":
        by = q.get("cancelledBy")
        return _problem(
            409,
            "not_open",
            state="cancelled",
            by=None if by in _NOT_A_PERSON else by,
            at=q.get("cancelledAt"),
            reason=q.get("reason"),
        )
    return _problem(409, "not_open", state=q["state"], by=q.get("answeredBy"), at=q.get("answeredAt"))


@router.post("/tasks/{task_id}/questions/{qid}/answer")
async def answer(task_id: str, qid: str, body: AnswerBody, auth: Annotated[AuthContext, Depends(person_only)]):
    ws = await _workspace(auth)
    me = auth.user_id
    task = await tasks_repo.get_task(ws, task_id)
    if task is None or not await tasks_access.can_view(task, me):
        raise _not_found()
    q = await questions_repo.get_question(ws, task_id, qid)
    if q is None:
        raise _not_found()
    if q["state"] != "open":
        if q["state"] == "answered" and q.get("clientAnswerId") == body.client_answer_id and q.get("answeredBy") == me:
            await _remember(ws, q, body, me)  # finishes a write a failed first press left
            return await _answered_view(ws, task, qid, me)  # the same press again
        raise _not_open(q)
    if task.get("status") in _CLOSED:
        # A closed task's Questions are cancelled when it ends; one left open (that cancel failed) is still never
        # answered, and no Temporal update is sent (it would start a fresh run for a finished task).
        raise _problem(409, "task_closed")
    if not await questions.can_answer_now(ws, q, me):
        raise _problem(403, "not_answerer")
    offered = {o["id"] for o in q.get("options") or []} or {"answer"}
    if body.option_id not in offered:
        raise _problem(400, "invalid_option")
    trigger_id = None
    if body.text:
        # The text rides on a Trigger row; the Temporal update carries ids only.
        trigger_id = ts.trigger_id("answer", qid, body.client_answer_id)
        await tasks_repo.put_trigger(
            ws,
            {
                "triggerId": trigger_id,
                "source": "answer",
                "ref": {"taskId": task_id, "qid": qid, "optionId": body.option_id, "text": body.text},
                "admission": "respond",
                "lane": "shared",
                "actorUserId": me,
                "receivedAt": utc_now_iso(),
            },
        )
    ref = AnswerRef(qid, body.option_id, trigger_id, me, body.client_answer_id, body.expect_version)
    try:
        await temporal_client.update_task(ws, task_id, "answer", ref)
    except temporal_client.TemporalUnavailable as err:
        raise _problem(503, "TEMPORAL_UNAVAILABLE", retryable=True) from err
    except Exception as err:  # noqa: BLE001 -- an update failure: the row says what happened
        codes = _failure_codes(err)
        now = await questions_repo.get_question(ws, task_id, qid)
        if now and now["state"] == "answered" and now.get("clientAnswerId") == body.client_answer_id:
            await _remember(ws, q, body, me)
            return await _answered_view(ws, task, qid, me)
        if now and now["state"] != "open":
            raise _not_open(now) from err
        if "task_closed" in codes or (await tasks_repo.get_task(ws, task_id) or {}).get("status") in _CLOSED:
            raise _problem(409, "task_closed") from err
        if {"not_answerer", "NotAnswerer"} & codes:
            raise _problem(403, "not_answerer") from err
        if "InvalidOption" in codes:
            raise _problem(400, "invalid_option") from err
        if "VersionConflict" in codes:
            raise _problem(409, "stale_version") from err
        raise _problem(502, "answer_failed") from err
    await _remember(ws, q, body, me)
    return await _answered_view(ws, task, qid, me)


async def _remember(ws: str, q: dict, body: AnswerBody, me: str) -> None:
    """A phone call's "didn't know" answered with text goes into the Agent page's Knowledge (voice_phone.call_ask)."""
    if q.get("subkind") == "didnt_know" and body.option_id == "answer" and (body.text or "").strip():
        from routers import voice_phone

        question = (q.get("preview") or {}).get("question") or q["title"]
        await voice_phone.remember_answer(ws, question, body.text.strip(), me)
