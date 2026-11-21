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

from core.auth import AuthContext, get_current_user, get_human_session, resolve_owner_id, security_optional
from core.engine import api
from core.engine.contract import AnswerIn, Principal
from core.repositories import questions_repo, tasks_repo
from core.services import tasks_access

router = APIRouter()


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "not_found"})


async def _workspace(auth: AuthContext) -> str:
    ws = resolve_owner_id(auth)
    return ws


@router.get("/needs-you")
async def needs_you(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    ws = await _workspace(auth)
    needs, nxt = await questions_repo.needs_for_user(ws, auth.user_id, cursor=cursor)
    found = await asyncio.gather(*(questions_repo.get_question(ws, n["taskId"], n["qid"]) for n in needs))
    open_qs = [q for q in found if q and q["state"] == "open" and auth.user_id in (q.get("answerers") or [])]
    return {"items": await api._views(open_qs, auth.user_id), "next": nxt}


@router.get("/tasks/{task_id}/questions")
async def task_questions(task_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    ws = await _workspace(auth)
    task = await tasks_repo.get_task(ws, task_id)
    if task is None or not await tasks_access.can_view(task, auth.user_id):
        raise _not_found()  # not visible reads the same as missing
    qs = sorted(await questions_repo.list_for_task(ws, task_id), key=lambda q: q["createdAt"])
    return {"items": await api._views(qs, auth.user_id)}


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


@router.post("/tasks/{task_id}/questions/{qid}/answer")
async def answer(task_id: str, qid: str, body: AnswerBody, auth: Annotated[AuthContext, Depends(person_only)]):
    req = AnswerIn(
        tenant_id=await _workspace(auth),
        task_id=task_id,
        qid=qid,
        principal=Principal(kind="member", id=auth.user_id),
        option_id=body.option_id,
        text=body.text,
        client_answer_id=body.client_answer_id,
        expect_version=body.expect_version,
    )
    return await api.http(api.answer(req))


async def submit_answer(ws: str, task_id: str, qid: str, me: str, body: AnswerBody) -> dict[str, Any]:
    """`me` answers Question `qid` (`core.engine.api.submit_answer`). Raises the route's HTTPExceptions."""
    return await api.http(api.submit_answer(ws, task_id, qid, me, body))
