"""The Router's state at the start of a voice talk (founder ruling 5 Nov 2026: the realtime voice model IS the
Router while a talk is live). Owner mode and the meeting agent start each talk from the same compact context: the
room's recent messages, the open and recently finished tasks, and a few memory facts. Bounded to `CONTEXT_MAX`
chars so it fits in the session's instructions. Each part fails soft: a talk starts even when memory is down."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from core.repositories import messaging_repo, tasks_repo
from core.services import memory_api, tasks_access

logger = logging.getLogger(__name__)

CONTEXT_MAX = 6000
MESSAGES = 20
TASKS = 12
FACTS = 5
RECENT = timedelta(days=3)  # a finished task older than this is history, not state
MEMORY_S = 3.0

Keep = Callable[[dict], bool]


def _flat(text: str, n: int) -> str:
    return " ".join((text or "").split())[:n]


def task_status(summary: dict) -> str:
    """working, needs_you, done or failed: what the voice says about a task."""
    if summary["shownGroup"] in ("waiting_on_you", "ready_for_review"):
        return "needs_you"
    if summary["status"] == "failed":
        return "failed"
    if summary["status"] in ("idle", "resolved") and not summary["watching"]:
        return "done"
    return "working"


async def task_view(ws: str, task: dict, viewer: str, room: str) -> dict:
    """{taskId, title, status, result}: the result is the line the room's chat shows (`tasks_access.result_line`)."""
    tree = await tasks_repo.get_task_tree(ws, task["taskId"])
    s = tasks_access.summarize(tree, viewer)
    line = tasks_access.result_line(tree, room, [viewer]) if tree["task"] else None
    return {"taskId": s["taskId"], "title": s["title"], "status": task_status(s), "result": line or ""}


async def _tasks(ws: str, viewer: str, room: str, keep: Keep) -> list[dict]:
    from routers.tasks import _search  # the task list's bounded walk, live work first

    cutoff = (datetime.now(UTC) - RECENT).isoformat()

    async def mine(t: dict) -> bool:
        fresh = t.get("status") not in ("idle", "resolved", "failed") or (t.get("updatedAt") or "") >= cutoff
        return fresh and keep(t) and await tasks_access.can_view(t, viewer)

    found, _ = await _search(ws, TASKS, mine)
    return list(await asyncio.gather(*(task_view(ws, t, viewer, room) for t in found)))


async def _messages(room: str) -> list[str]:
    rows = await messaging_repo.list_messages_before(room, 10**19, MESSAGES)
    return [
        f"{(r.get('author') or {}).get('displayName') or (r.get('author') or {}).get('kind') or 'someone'}: "
        f"{_flat(r['text'], 280)}"
        for r in rows
        if (r.get("text") or "").strip() and not r.get("progress") and not r.get("deletedAt")
    ]


async def _facts(ws: str, memory_user: str | None, room: str) -> list[str]:
    page = await asyncio.wait_for(
        memory_api.recall_page(
            ws,
            memory_user,
            "recent decisions and open work",
            budget_tokens=800,
            use_expand=False,
            use_rerank=False,
            audience={"kind": "room", "roomId": room},
        ),
        MEMORY_S,
    )
    return [_flat(r["content"], 300) for r in page.get("results", [])[:FACTS]]


async def _soft(part: str, coro: Awaitable[list]) -> list:
    try:
        return await coro
    except Exception:  # noqa: BLE001 -- any part's failure leaves that part out, never the talk
        logger.warning("voice context: %s left out", part, exc_info=True)
        return []


def render(messages: list[str], tasks: list[dict], facts: list[str]) -> str:
    """The context as text, oldest messages dropped first to fit `CONTEXT_MAX`."""
    task_lines = [
        f"- [{t['taskId']}] {t['title']}: {t['status'].replace('_', ' ')}"
        + (f" ({t['result']})" if t["result"] else "")
        for t in tasks
    ]
    messages = list(messages)
    while True:
        parts = [
            ("Recent chat, oldest first:", messages),
            ("Tasks (id in brackets):", task_lines),
            ("Memory:", [f"- {f}" for f in facts]),
        ]
        text = "\n\n".join(f"{head}\n" + "\n".join(lines) for head, lines in parts if lines)
        if len(text) <= CONTEXT_MAX or not messages:
            return text[:CONTEXT_MAX]
        messages.pop(0)


async def context(ws: str, viewer: str, room: str, keep: Keep, memory_user: str | None, chat_room: str) -> str:
    """`viewer` sees the tasks `keep` takes; `chat_room` gives the recent messages; `memory_user` adds that
    person's private memory (None: the workspace's, when others hear it)."""
    messages, tasks, facts = await asyncio.gather(
        _soft("messages", _messages(chat_room)),
        _soft("tasks", _tasks(ws, viewer, room, keep)),
        _soft("memory", _facts(ws, memory_user, room)),
    )
    return render(messages, tasks, facts)
