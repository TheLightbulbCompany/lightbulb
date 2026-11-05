"""Recall links to tasks (spec 07 §3.4, contracts-v2 §12; review C16).

A link names a task the page's evidence points at: the task arm's cards first, then each turn's
``taskId`` tag, then (for an untagged turn) the task its thread belongs to through the ``THREAD#`` row,
which keeps its task id after the subscription ends. Every link is read from the **live** row and kept
only when ``link_visible`` passes for the asking audience; the card's text is never shown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from core import tasks_schema as ts
from core.repositories import messaging_repo, tasks_repo
from core.services import task_conversation, tasks_access

MAX_LINKS = 10
MAX_THREADS = 3


async def link_visible(task: dict, audience: dict) -> bool:
    """person → ``can_view``; room → a workspace task, or a room task of that same room; workspace → a
    workspace task. Hidden never. No "any channel" widening (C16)."""
    if not task or task.get("hidden"):
        return False
    kind = audience.get("kind")
    shown = task.get("audience") or {}
    if kind == "person":
        return await tasks_access.can_view(task, audience.get("userId") or "")
    if kind == "room":
        return shown.get("kind") == "workspace" or (
            shown.get("kind") == "room" and bool(shown.get("roomId")) and shown["roomId"] == audience.get("roomId")
        )
    return kind == "workspace" and shown.get("kind") == "workspace"


async def _thread_visible(key: str, audience: dict) -> bool:
    """Only room threads are shown, and only to that room or one of its members."""
    if not key.startswith("room:"):
        return False
    room_id = key.split(":")[1]
    if audience.get("kind") == "room":
        return audience.get("roomId") == room_id
    if audience.get("kind") == "person":
        return await messaging_repo.get_member(room_id, "human", audience.get("userId") or "") is not None
    return False


async def _link(ws: str, task_id: str, why: str, audience: dict) -> dict | None:
    tree = await tasks_repo.get_task_tree(ws, task_id)
    task = tree["task"]
    if not await link_visible(task, audience):
        return None
    subs = sorted(tree["subscriptions"], key=lambda s: not s.get("active"))  # active threads first
    threads = [s["threadKey"] for s in subs if await _thread_visible(s["threadKey"], audience)][:MAX_THREADS]
    return {
        "kind": "task",
        "id": task["taskId"],
        "identifier": task["identifier"],
        "title": task["title"],
        "status": task["status"],
        "specialist": task.get("specialist"),
        "updatedAt": task.get("updatedAt"),
        "why": why,
        "threads": [{"kind": "thread", "id": t} for t in threads],
    }


async def resolve_links(ws: str, results: list[dict], task_hits: list[str], audience: dict) -> list[dict]:
    """Links for a page, in rank order (task arm, then turns in page order), at most ``MAX_LINKS``."""
    order: list[tuple[str, str]] = [(t, "task") for t in task_hits]
    untagged = [r["threadId"] for r in results if r.get("kind") == "turn" and not r.get("taskId") and r.get("threadId")]
    by_thread: dict[str, str] = {}
    if untagged:
        rows = await tasks_repo.batch_get(ws, [ts.thread_sk(k) for k in untagged if not k.startswith("h:")])
        by_thread = {r["threadKey"]: r["taskId"] for r in rows}
    for r in results:
        if r.get("kind") != "turn":
            continue
        task_id = r.get("taskId") or by_thread.get(r.get("threadId") or "")
        if task_id:
            order.append((task_id, "turn"))
    picked: dict[str, str] = {}
    for task_id, why in order:
        picked.setdefault(task_id, why)
        if len(picked) == MAX_LINKS:
            break
    links = await asyncio.gather(*(_link(ws, t, why, audience) for t, why in picked.items()))
    return [lk for lk in links if lk]


async def read_link(ws: str, task_id: str, audience: dict) -> dict | None:
    """The Router's second hop: the task as this audience may see it, or None (route answers 404)."""
    tree = await tasks_repo.get_task_tree(ws, task_id)
    if not await link_visible(tree["task"], audience):
        return None
    viewer = (audience.get("userId") or "") if audience.get("kind") == "person" else ""
    threads = [s["threadKey"] for s in tree["subscriptions"] if await _thread_visible(s["threadKey"], audience)]
    return {
        "task": tasks_access.summarize(tree, viewer),
        "threads": threads[:MAX_THREADS],
        "lastResult": await task_conversation.last_result_text(ws, task_id),
    }


BOARD_ROWS = 10
BOARD_DONE_DAYS = 14
_BOARD_SCAN = 50  # rows read per status before the visibility filter


async def _visible_rows(ws: str, status: str, audience: dict, since: str = "") -> list[dict]:
    rows, _ = await tasks_repo.list_tasks_by_status(ws, status, limit=_BOARD_SCAN)
    rows = [r for r in rows if str(r.get("updatedAt") or "") >= since]
    keep = await asyncio.gather(*(link_visible(r, audience) for r in rows))
    return [r for r, ok in zip(rows, keep, strict=True) if ok][:BOARD_ROWS]


def _board_row(task: dict, dated: bool = False) -> str:
    title = " ".join(str(task.get("title") or "").split())[:120]
    day = str(task.get("updatedAt") or "")[:10]
    return f"{task.get('identifier') or ''} {title}".strip() + (f" ({day})" if dated and day else "")


async def board_text(ws: str, audience: dict) -> str:
    """The super-agent task board for one audience, in the plugin's board wording (it replaces the
    Paperclip read on super-agent turns): working and waiting, and resolved in the last 14 days."""
    since = (datetime.now(UTC) - timedelta(days=BOARD_DONE_DAYS)).isoformat()
    doing, waiting, done = await asyncio.gather(
        _visible_rows(ws, "working", audience),
        _visible_rows(ws, "waiting", audience),
        _visible_rows(ws, "resolved", audience, since),
    )
    lines = []
    if doing:
        lines.append("In progress: " + "; ".join(map(_board_row, doing)))
    if waiting:
        lines.append("Waiting: " + "; ".join(map(_board_row, waiting)))
    if done:
        lines.append(f"Done in the last {BOARD_DONE_DAYS} days: " + "; ".join(_board_row(t, True) for t in done))
    if not lines:
        return ""
    return (
        f"Task board, partial: in progress, waiting and recently done only, up to {BOARD_ROWS} each, not the "
        "whole board. Live: it wins over a remembered fact that says otherwise.\n" + "\n".join(lines)
    )
