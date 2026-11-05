"""Super-agent Questions for people (spec 05 §5.1; contracts-v2 §3, §5, §14.2).

`GET /api/v1/needs-you` (the viewer's open Questions, from `NEEDS#{me}#`) and `GET /api/v1/tasks/{taskId}/questions`
(a task's history, answered and cancelled included; visible through `tasks_access.can_view`). Both are 404 unless the
workspace's switch is on. Mounted before `routers/tasks.py` so `/tasks/{id}/questions` is never read as a task route.

The answer route (`POST …/questions/{qid}/answer`) lands with the `answer` update on `TaskWorkflow` (wave2-Q Task 3).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import questions_repo, tasks_repo
from core.services import super_agent_flags, tasks_access
from core.services.messaging import human_display_name

router = APIRouter()

_STYLES = {"deny": "danger", "request_changes": "secondary"}


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "not_found"})


async def _workspace(auth: AuthContext) -> str:
    ws = resolve_owner_id(auth)
    if not await super_agent_flags.flags_for(ws):
        raise _not_found()
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
        waiting = "Waiting on " + " or ".join(names.get(u, u) for u in answerers)

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
        "cancelledBy": person(q.get("cancelledBy")),
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
