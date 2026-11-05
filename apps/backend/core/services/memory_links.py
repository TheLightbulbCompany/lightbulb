"""Recall links to tasks (spec 07 §3.4, contracts-v2 §12; review C16).

A link names a task the page's evidence points at: the task arm's cards first, then each turn's
``taskId`` tag, then (for an untagged turn) the task its thread belongs to through the ``THREAD#`` row,
which keeps its task id after the subscription ends. Every link is read from the **live** row and kept
only when ``link_visible`` passes for the asking audience; the card's text is never shown.
"""

from __future__ import annotations

import asyncio

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
