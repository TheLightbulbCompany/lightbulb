"""Shared helpers for Lane O's acceptance scenarios (spec 09 §8.3; G1's `Ctx` / `Result`).

Rooms, messages and tasks go through the public API as Sam or Priya (`ctx.api(user)`); rows come from the
dev-only `ctx.tasks_rows(ws)` read. Every helper returns plain data and raises nothing but the API's errors.
"""

from __future__ import annotations

import uuid
from typing import Any

from core import tasks_schema as ts
from core.services.questions import MONEY_SUBKINDS

#: Upper bound the scenarios WAIT for the Router (cold start 30-60 s; its turns are sequential per workspace).
#: The waits return as soon as the thing is there; the observed seconds go in the evidence.
ROUTER_REPLY_S = 180
#: A9's recipient: matches the +clerk_test rule but is NOT a workspace member, so Auto mode still asks (#4891).
OUTSIDER_EMAIL = "acceptance-outsider+clerk_test@thelightbulb.company"
START_S = 15
EVENT_S = 3
ACK_S = 5
ASKS = (
    "Four things: 1) build a signup page for the workshop with our purple theme, 2) email last week's attendees "
    f"(only {OUTSIDER_EMAIL}, this is a test) about the new date, 3) estimate what Stripe "
    "fees we'd pay on 200 tickets, 4) and buy a domain for the workshop signup page (around $15)."
)


async def create_room(ctx: Any, user: str, name: str, members: list[dict]) -> dict:
    api = ctx.api(user)
    body = {"clientRoomId": str(uuid.uuid4()), "name": name, "kind": "channel", "members": members}
    r = await api.post("/api/v1/messaging/rooms", json=body)
    r.raise_for_status()
    return r.json()


async def post(ctx: Any, user: str, room_id: str, text: str, *, thread_root: str | None = None, mentions=()) -> dict:
    api = ctx.api(user)
    body = {
        "clientMessageId": str(uuid.uuid4()),
        "text": text,
        "mentions": [{"kind": "agent", "id": m} for m in mentions],
        "replyToMessageId": thread_root,  # a real thread reply names its parent and its root
        "threadRootMessageId": thread_root,
    }
    r = await api.post(f"/api/v1/messaging/rooms/{room_id}/messages", json=body)
    r.raise_for_status()
    return r.json()


async def messages(ctx: Any, user: str, room_id: str, *, limit: int = 50) -> list[dict]:
    r = await ctx.api(user).get(f"/api/v1/messaging/rooms/{room_id}/messages", params={"limit": limit})
    r.raise_for_status()
    data = r.json()
    return data.get("messages") or data.get("items") or []


async def poll_messages(ctx: Any, user: str, room_id: str, pred, timeout_s: float = 30, *, limit: int = 50):
    """Room messages (thread replies included) re-read every 0.5 s until ``pred(rows)`` holds or ``timeout_s``;
    the last read either way. A task's row moves before its post lands (`end_attempt`, the run card)."""
    for _ in range(int(timeout_s * 2)):
        rows = await messages(ctx, user, room_id, limit=limit)
        if pred(rows):
            return rows
        await ctx.sleep(0.5)
    return await messages(ctx, user, room_id, limit=limit)


async def room(ctx: Any, user: str, room_id: str) -> dict:
    r = await ctx.api(user).get(f"/api/v1/messaging/rooms/{room_id}")
    r.raise_for_status()
    return r.json()


def agent_posts(rows: list[dict]) -> list[dict]:
    return [m for m in rows if (m.get("author") or {}).get("kind") == "agent"]


def rows_of(rows: list[dict], kind: str) -> list[dict]:
    return [r for r in rows if r.get("type") == kind]


def tasks_from(rows: list[dict], message_id: str | None = None) -> list[dict]:
    """TASK rows the Router made for one message (its trigger id), or all of them."""
    tasks = rows_of(rows, "task")
    if message_id is None:
        return tasks
    return [t for t in tasks if t.get("triggerId") == ts.trigger_id("chat", message_id)]


def worker_authored(rows: list[dict], front_agent_id: str) -> list[dict]:
    """Room messages whose author is an agent other than Lightbulb (contracts §6: there must be none)."""
    return [m for m in agent_posts(rows) if m["author"].get("id") != front_agent_id]


async def front_agent(ctx: Any, user: str, room_id: str) -> dict | None:
    detail = await room(ctx, user, room_id)
    agents = [m for m in detail.get("members", []) if m.get("kind") == "agent"]
    return agents[0] if len(agents) == 1 else None


async def task_detail(ctx: Any, user: str, task_id: str) -> dict:
    r = await ctx.api(user).get(f"/api/v1/tasks/{task_id}")
    r.raise_for_status()
    return r.json()


async def task_questions(ctx: Any, user: str, task_id: str) -> list[dict]:
    """The task's QuestionViews as ``user`` sees them (``canAnswer``, ``waitingLabel`` are per viewer)."""
    r = await ctx.api(user).get(f"/api/v1/tasks/{task_id}/questions")
    r.raise_for_status()
    return r.json()["items"]


class RefusedToAnswer(Exception):
    """A scenario tried to answer a Question that spends money (or the purchase task's): never pressed."""


async def answer_question(ctx: Any, user: str, task_id: str, qid: str, option_id: str, *, key: str):
    """The raw response: callers assert on the status (403 for a non-answerer). The ONE place a Question is
    answered, so the money guard lives here: fetch it first and refuse (raise, no POST) for a money subkind
    or for the purchase task A2 started."""
    view = next((q for q in await task_questions(ctx, user, task_id) if q["id"] == qid), None)
    if view is None or view.get("subkind") in MONEY_SUBKINDS or task_id == ctx.state.get("purchaseTaskId"):
        raise RefusedToAnswer(f"{task_id}/{qid}: {'unknown' if view is None else view.get('subkind')} Question")
    body = {"optionId": option_id, "clientAnswerId": key}
    return await ctx.api(user).post(f"/api/v1/tasks/{task_id}/questions/{qid}/answer", json=body)


def question_row(rows: list[dict], task_id: str, kinds: tuple[str, ...], state: str = "open") -> dict | None:
    return next(
        (
            q
            for q in rows_of(rows, "question")
            if q.get("taskId") == task_id and q.get("kind") in kinds and q.get("state") == state
        ),
        None,
    )
