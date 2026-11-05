"""Super-agent task API (wave2-O Task 8; C15; contracts-v2 §4–§7).

Lists, detail, actions, trace and read for tasks. `tasks_access.can_view` is the only visibility rule; every action goes
through `TaskWorkflow` (`core.temporal.client`), never a direct row write. Every route is 404 while the workspace's
switch is off. Mounted after `routers/questions.py` so `/tasks/{id}/questions` stays Q's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import effects_repo, messaging_repo, questions_repo, tasks_repo
from core.services import chat_gate, runtime_runs, super_agent_flags, task_conversation, tasks_access
from core.services.messaging import human_display_name
from core.temporal import client as temporal_client
from core.temporal.types import ActionRequest, StopRequest

router = APIRouter()
logger = logging.getLogger(__name__)

# Listing walks the `by-status` GSI one status at a time, live work first.
_STATUSES = ("waiting", "working", "queued", "proposed", "idle", "failed", "resolved")
# `needsYou=true`: the viewer's Needs you groups, complete (Home and its rail badge count these).
_NEEDS_YOU_GROUPS = frozenset({"waiting_on_you", "ready_for_review"})
# Its bounds: index pages read per source (100 rows each), and task trees read at once.
_NEEDS_YOU_PAGES = 10
_NEEDS_YOU_FETCHES = 10
_GATED = frozenset({"start", "retry", "reopen"})  # a new attempt: today's chat gate first (§14.10)

Auth = Annotated[AuthContext, Depends(get_current_user)]


class VersionBody(BaseModel):
    version: int


class BulkStartBody(BaseModel):
    task_ids: list[str] = Field(alias="taskIds", max_length=100)


def _err(http_status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, **extra})


async def _workspace(auth: AuthContext) -> str:
    ws = resolve_owner_id(auth)
    if not await super_agent_flags.flags_for(ws):
        raise _err(404, "not_found")
    return ws


async def _visible_tree(ws: str, task_id: str, user_id: str) -> dict:
    tree = await tasks_repo.get_task_tree(ws, task_id)
    if tree["task"] is None:
        raise _err(404, "not_found")
    if not await tasks_access.can_view(tree["task"], user_id):
        raise _err(404, "not_found")  # hidden reads as missing: no existence leak (lead ruling, 5 Nov)
    return tree


async def _summary(tree: dict, viewer: str) -> dict:
    ids = list(
        dict.fromkeys(u for q in tree["questions"] if q.get("state") == "open" for u in q.get("answerers") or [])
    )
    names = dict(zip(ids, await asyncio.gather(*(human_display_name(u) for u in ids))))
    return tasks_access.summarize({**tree, "userNames": names}, viewer)


@router.get("/tasks")
async def list_tasks(
    auth: Auth,
    scope: str = "mine",
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    needs_you: Annotated[bool, Query(alias="needsYou")] = False,
):
    ws = await _workspace(auth)
    me = auth.user_id
    room = scope.removeprefix("room:") if scope.startswith("room:") else None
    if room is not None:
        if await messaging_repo.get_member(room, "human", me) is None:
            raise _err(403, "forbidden")
    elif scope not in ("mine", "workspace"):
        raise _err(400, "invalid_scope")

    async def keep(t: dict) -> bool:
        audience = t.get("audience") or {}
        if room is not None and audience.get("roomId") != room:
            return False
        if scope == "mine" and t.get("ownerUserId") != me and me not in (audience.get("userIds") or []):
            return False
        return await tasks_access.can_view(t, me)

    if needs_you:
        return {"tasks": await _needs_you(ws, me, keep), "next": None}

    try:
        i, _, inner = (cursor or "0~").partition("~")
        i, inner = int(i), inner or None
        found: list[dict] = []
        while True:
            rows, nxt = await tasks_repo.list_tasks_by_status(ws, _STATUSES[i], limit=limit - len(found), cursor=inner)
            found += [t for t in rows if await keep(t)]
            i, inner = (i, nxt) if nxt else (i + 1, None)
            if i >= len(_STATUSES):
                next_cursor = None
                break
            if len(found) >= limit:
                next_cursor = f"{i}~{inner or ''}"
                break
    except (ValueError, IndexError) as exc:
        raise _err(400, "invalid_cursor") from exc
    # ponytail: one tree query per listed task (≤100); a summary index if lists get hot.
    trees = await asyncio.gather(*(tasks_repo.get_task_tree(ws, t["taskId"]) for t in found))
    return {"tasks": [await _summary(tree, me) for tree in trees if tree["task"]], "next": next_cursor}


async def _needs_you(ws: str, me: str, keep) -> list[dict]:
    """Every task waiting on `me` or ready for their review, unpaged: the tasks behind `me`'s open questions (the
    `NEEDS#` index) plus the failed tasks `me` owns (Retry is theirs). Kept by the scope's own rule, then by the same
    `shownGroup` the list shows, so a count and its rows can never disagree."""
    ids: dict[str, None] = {}

    async def drain(source: str, page) -> None:
        cursor: str | None = None
        for _ in range(_NEEDS_YOU_PAGES):
            rows, cursor = await page(cursor)
            ids.update(dict.fromkeys(r["taskId"] for r in rows))
            if not cursor:
                return
        logger.warning("needs-you: %s capped at %d pages (ws=%s user=%s)", source, _NEEDS_YOU_PAGES, ws, me)

    await drain("questions", lambda c: questions_repo.needs_for_user(ws, me, cursor=c, limit=100))
    # The failed tasks `me` owns, filtered in the query (`ownerUserId`); the walk itself is still the workspace's
    # failed tasks, so it is capped. ponytail: an owner-keyed index if failures pile up past the cap.
    await drain(
        "failed", lambda c: tasks_repo.list_tasks_by_status(ws, "failed", limit=100, cursor=c, owner_user_id=me)
    )
    gate = asyncio.Semaphore(_NEEDS_YOU_FETCHES)

    async def tree(tid: str) -> dict:
        async with gate:
            return await tasks_repo.get_task_tree(ws, tid)

    trees = await asyncio.gather(*(tree(tid) for tid in ids))
    out = []
    for found in trees:
        if found["task"] is None or not await keep(found["task"]):
            continue
        summary = await _summary(found, me)
        if summary.get("shownGroup") in _NEEDS_YOU_GROUPS:
            out.append(summary)
    return sorted(out, key=lambda t: t.get("updatedAt") or "", reverse=True)


async def _act(ws: str, task_id: str, action: str, user_id: str, version: int | None) -> dict:
    tree = await _visible_tree(ws, task_id, user_id)
    task = tree["task"]
    if version is not None and task["version"] != version:
        raise _err(409, "version_conflict", version=task["version"])
    if action == "start" and task["status"] != "proposed":
        raise _err(409, "illegal_state", status=task["status"])
    if action in _GATED:
        gate = await chat_gate.gate_chat(user_id=user_id, owner_id=ws, session_key=f"task:{task_id}")
        if gate.get("blocked"):
            raise _err(402, gate.get("code") or "blocked", message=gate.get("message"))
    try:
        if action == "start":  # a signal, not an update: start (or keep) the workflow, then consent
            await temporal_client.start_task_workflow(ws, task_id, "propose_start")
            await temporal_client.task_handle(ws, task_id).signal("start")
        elif action == "stop":
            await temporal_client.update_task(ws, task_id, "stop", StopRequest(by_user_id=user_id))
        else:
            await temporal_client.update_task(ws, task_id, action, ActionRequest(user_id, expected_version=version))
    except WorkflowUpdateFailedError as exc:  # the workflow's validator refused this state
        raise _err(409, "illegal_state") from exc
    except (temporal_client.TemporalUnavailable, RPCError) as exc:
        raise _err(503, "TEMPORAL_UNAVAILABLE", retryable=True) from exc
    return await _summary(await tasks_repo.get_task_tree(ws, task_id), user_id)


@router.post("/tasks/start")
async def bulk_start(body: BulkStartBody, auth: Auth):
    """Start each proposed task the caller can see; others (missing, not visible, not proposed) are skipped."""
    ws = await _workspace(auth)
    started = []
    for task_id in dict.fromkeys(body.task_ids):
        try:
            started.append(await _act(ws, task_id, "start", auth.user_id, None))
        except HTTPException as exc:
            if exc.status_code not in (403, 404, 409):
                raise
    return {"tasks": started}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, auth: Auth):
    ws = await _workspace(auth)
    tree = await _visible_tree(ws, task_id, auth.user_id)
    room_id = await task_conversation.ensure_task_room(ws, tree["task"], seat_viewer=auth.user_id)
    effects = await effects_repo.effects_of_task(ws, task_id)
    return {
        "task": await _summary({**tree, "effects": effects}, auth.user_id),
        "attempts": tree["attempts"],
        "questions": tree["questions"],
        "effects": effects,
        "subscriptions": tree["subscriptions"],
        "taskRoomId": room_id,
    }


@router.post("/tasks/{task_id}/read", status_code=204)
async def mark_read(task_id: str, auth: Auth):
    """G26: no read state exists (contracts §3 cut `READ#`), so this is a visibility-checked no-op. Declared before
    `/{action}` so `read` is not taken for an action."""
    ws = await _workspace(auth)
    await _visible_tree(ws, task_id, auth.user_id)
    return Response(status_code=204)


@router.post("/tasks/{task_id}/{action}")
async def task_action(
    task_id: str,
    action: Literal["start", "dismiss", "stop", "resolve", "reopen", "retry"],
    body: VersionBody,
    auth: Auth,
):
    return await _act(await _workspace(auth), task_id, action, auth.user_id, body.version)


@router.get("/tasks/{task_id}/trace")
async def task_trace(task_id: str, auth: Auth, attempt: Annotated[int | None, Query(ge=1)] = None):
    """The Attempt's live `recentTools` from its run on the bridge (latest attempt by default)."""
    ws = await _workspace(auth)
    tree = await _visible_tree(ws, task_id, auth.user_id)
    attempts = {a["n"]: a for a in tree["attempts"]}
    n = attempt or max(attempts, default=None)
    if n not in attempts:
        raise _err(404, "not_found")
    row = attempts[n]
    identity = runtime_runs.RunIdentity.for_attempt(ws, task_id, {**row, "n": n})
    try:
        run = await runtime_runs.get_run(ws, identity)
    except (runtime_runs.RunLost, runtime_runs.RunRefused):
        return {"attempt": n, "recentTools": []}
    except runtime_runs.RuntimeBusy as exc:
        raise _err(503, "RUNTIME_UNAVAILABLE", retryable=True) from exc
    return {"attempt": n, "recentTools": run.recent_tools}
