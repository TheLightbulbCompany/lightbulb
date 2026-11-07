"""The workspace task board for the Lightbulb MCP tools.

Both MCP surfaces call these with an AuthContext: the hosted MCP (OAuth person) and the
paired-Mac device routes (routers/messaging.py, /local-sessions/device/*). The board is the tasks table
(contracts-v2 §14.9): reads go through `tasks_access.can_view`, and a change is a signal to `TaskWorkflow`
(`client.update_task`) or a note in the task's room — never a row write.

"""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id

OPEN_STATUSES = ("backlog", "todo", "in_progress", "in_review", "blocked")
STATUSES = (*OPEN_STATUSES, "done", "cancelled")
PRIORITIES = ("critical", "high", "medium", "low")
NOTE_MAX = 4000


#: Board status words → task statuses (contracts §4). `blocked` ≈ waiting on a person; `in_review` ≈ idle with output.
_SA_STATUS = {
    "backlog": (),  # nothing waits to be started since 5 Nov 2026: requests always start
    "todo": ("queued",),
    "in_progress": ("working",),
    "blocked": ("waiting",),
    "in_review": ("idle",),
    # A stopped task is `resolved` + statusReason=stopped; a `proposed` row (before 5 Nov 2026) reads as closed.
    "done": ("resolved", "proposed"),
    "cancelled": ("failed",),
}
_SA_OPEN = ("queued", "working", "waiting", "idle")
# ponytail: one page per status, newest first; a board past 50 open tasks per status needs the cursor.
_SA_SCAN = 50
_IDENTIFIER = re.compile(r"^LB-\d{1,9}$")


async def _sa_rows(ws: str, user_id: str, statuses: tuple[str, ...]) -> list[dict]:
    from core.repositories import tasks_repo
    from core.services import tasks_access

    rows: list[dict] = []
    for status in dict.fromkeys(statuses):
        page, _ = await tasks_repo.list_tasks_by_status(ws, status, limit=_SA_SCAN)
        rows += [r for r in page if not r.get("hidden")]
    keep = [await tasks_access.can_view(r, user_id) for r in rows]
    return [r for r, ok in zip(rows, keep, strict=True) if ok]


async def _sa_task(ws: str, user_id: str, task: str) -> dict:
    """The task tree by id or identifier (LB-12), only when the caller may see it (404 otherwise)."""
    from core.repositories import tasks_repo
    from core.services import tasks_access

    if not isinstance(task, str) or len(task) > 64:
        raise HTTPException(status_code=404, detail="Task not found")
    if _IDENTIFIER.match(task):
        row = next(
            (
                r
                for r in await _sa_rows(ws, user_id, (*_SA_OPEN, "resolved", "failed", "proposed"))
                if r.get("identifier") == task
            ),
            None,
        )
        task = row["taskId"] if row else ""
    try:
        uuid.UUID(task)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    tree = await tasks_repo.get_task_tree(ws, task)
    if tree["task"] is None or tree["task"].get("hidden") or not await tasks_access.can_view(tree["task"], user_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return tree


async def _sa_names(ws: str) -> dict[str, str]:
    from core.services import messaging

    return {a["id"]: a.get("name") or "" for a in await messaging.load_agent_roster(ws)}


def _sa_summary(row: dict, names: dict[str, str]) -> dict:
    """One task as the MCP tools show it (`priority` has no equivalent)."""
    board = next((word for word, kinds in _SA_STATUS.items() if row.get("status") in kinds), row.get("status"))
    specialist = row.get("specialist")
    return {
        "identifier": row.get("identifier"),
        "id": row.get("taskId"),
        "title": row.get("title"),
        "status": board,
        "priority": None,
        "assignee": names.get(specialist, "Lightbulb") if specialist else "Lightbulb",
        "updatedAt": row.get("updatedAt"),
    }


async def _sa_find(ws: str, user_id: str, query: str, status: str, assignee: str, limit: int) -> dict:
    wanted = [s.strip() for s in status.split(",") if s.strip()] if status else list(OPEN_STATUSES)
    if any(s not in STATUSES for s in wanted):
        raise HTTPException(status_code=422, detail=f"status must be one or more of {', '.join(STATUSES)}")
    names = await _sa_names(ws)
    rows = await _sa_rows(ws, user_id, tuple(k for w in wanted for k in _SA_STATUS[w]))
    if assignee.strip():
        needle = assignee.casefold().strip()
        exact = [i for i, n in names.items() if n.casefold() == needle]
        matches = exact or [i for i, n in names.items() if needle in n.casefold()]
        if len(matches) != 1:
            return {"tasks": [], "note": f"{len(matches)} agents match assignee {assignee!r}; use an exact agent name."}
        rows = [r for r in rows if r.get("specialist") == matches[0]]
    if query.strip():
        needle = query.casefold().strip()[:200]
        rows = [r for r in rows if needle in f"{r.get('title') or ''} {r.get('description') or ''}".casefold()]
    rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return {"tasks": [_sa_summary(r, names) for r in rows[: max(1, min(limit, 100))]]}


async def _sa_read(ws: str, user_id: str, task: str, comments: int) -> dict:
    from core.repositories import messaging_repo
    from core.tasks_schema import task_room_id

    tree = await _sa_task(ws, user_id, task)
    row = tree["task"]
    names = await _sa_names(ws)
    lines: list[dict] = []
    if comments > 0:
        room_id = task_room_id(row["taskId"])
        meta = await messaging_repo.get_room(room_id)
        if meta is not None:
            recent = await messaging_repo.list_messages_before(
                room_id, int(meta.get("lastSeq") or 0) + 1, max(0, min(comments, 50))
            )
            for m in sorted(recent, key=lambda m: m.get("createdAt") or ""):
                if m.get("deletedAt"):
                    continue
                author = m.get("author") or {}
                lines.append(
                    {
                        "author": names.get(author.get("id"), author.get("displayName") or "Lightbulb")
                        if author.get("kind") == "agent"
                        else author.get("displayName") or "a person",
                        "createdAt": m.get("createdAt"),
                        "body": messaging_repo.agent_text(m.get("editedText") or m.get("text") or ""),
                    }
                )
    return {
        **_sa_summary(row, names),
        "description": row.get("description") or "",
        "blockedBy": [],
        "comments": lines,
    }


async def _sa_update(ws: str, user_id: str, task: str, status: str | None, priority: str | None, note: str) -> dict:
    """A status change is a signal to the task's workflow (resolve / stop), a note is a line in its room
    (contracts §14.9: never a direct row write)."""
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.service import RPCError

    from core.repositories import tasks_repo
    from core.services import messaging, task_conversation
    from core.tasks_schema import task_room_id
    from core.temporal import client as temporal_client
    from core.temporal.types import ActionRequest, StopRequest

    if priority is not None:
        raise HTTPException(status_code=422, detail="Tasks here have no priority; ask Lightbulb to start or stop one.")
    if status is not None and status not in ("done", "cancelled"):
        raise HTTPException(
            status_code=422, detail="status can only be set to done or cancelled; Lightbulb moves tasks otherwise."
        )
    tree = await _sa_task(ws, user_id, task)
    task_id = tree["task"]["taskId"]
    try:
        if status == "done":
            await temporal_client.update_task(ws, task_id, "resolve", ActionRequest(user_id))
        elif status == "cancelled":
            await temporal_client.update_task(ws, task_id, "stop", StopRequest(by_user_id=user_id))
    except WorkflowUpdateFailedError as exc:
        raise HTTPException(status_code=409, detail="The task is not in a state that allows that.") from exc
    except (temporal_client.TemporalUnavailable, RPCError) as exc:
        raise HTTPException(status_code=503, detail="Tasks are briefly unavailable; try again.") from exc
    if note:
        who = await messaging.human_display_name(user_id)
        await task_conversation.ensure_task_room(ws, tree["task"])
        await task_conversation.post_as_lightbulb(
            ws,
            task_room_id(task_id),
            text=f"**{who}** noted: {note}",
            event_id=f"task-note:{uuid.uuid4()}",
            kind="note",
        )
    return _sa_summary((await tasks_repo.get_task_tree(ws, task_id))["task"], await _sa_names(ws))


async def find_tasks(auth: AuthContext, query: str = "", status: str = "", assignee: str = "", limit: int = 25) -> dict:
    return await _sa_find(resolve_owner_id(auth), auth.user_id, query, status, assignee, limit)


async def read_task(auth: AuthContext, task: str, comments: int = 10) -> dict:
    return await _sa_read(resolve_owner_id(auth), auth.user_id, task, comments)


async def update_task(
    auth: AuthContext, task: str, status: str | None = None, priority: str | None = None, append_note: str | None = None
) -> dict:
    """A status change or a note on the task (see `_sa_update`)."""
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")
    if priority is not None and priority not in PRIORITIES:
        raise HTTPException(status_code=422, detail=f"priority must be one of {', '.join(PRIORITIES)}")
    note = (append_note or "").strip()
    if len(note) > NOTE_MAX:
        raise HTTPException(status_code=422, detail=f"append_note is longer than {NOTE_MAX} characters")
    if status is None and priority is None and not note:
        raise HTTPException(status_code=422, detail="Nothing to change: give status, priority or append_note.")
    return await _sa_update(resolve_owner_id(auth), auth.user_id, task, status, priority, note)
