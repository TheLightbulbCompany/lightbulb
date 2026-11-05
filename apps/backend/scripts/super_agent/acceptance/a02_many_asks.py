"""A2 — ask for several things at once: one reply, every ask started, the estimate inline (spec 09 §8.3).

A quick factual answer (the Stripe fee estimate) stays in the Router's reply line: no task, no card. Since 5 Nov 2026
nothing is proposed: the domain purchase starts like the rest and stops at an approval Question, which is never
answered here; the purchase task is then stopped."""

from __future__ import annotations

import re

from core import tasks_schema as ts
from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import (
    ROUTER_REPLY_S,
    ASKS,
    front_agent,
    messages,
    post,
    question_row,
    rows_of,
    task_detail,
    tasks_from,
    worker_authored,
)


SUB_S = 30
STARTED = ("queued", "working", "waiting", "idle", "resolved")  # the worker may already be past working
ESTIMATE = re.compile(r"[$%]\s*\d|\d\s*%|\d+\s*(?:dollars|cents)", re.I)  # a figure in the reply line
PURCHASE = re.compile(r"domain", re.I)  # the paid ask's task title
PURCHASE_Q_S = 180


async def _stop(ctx: Ctx, task_id: str) -> int | None:
    """Best-effort Stop of the purchase task; its HTTP status, or None when even that failed."""
    try:
        version = (await task_detail(ctx, "sam", task_id))["task"]["version"]
        return (await ctx.api("sam").post(f"/api/v1/tasks/{task_id}/stop", json={"version": version})).status_code
    except Exception:  # noqa: BLE001 -- the original error (if any) is the one to surface
        return None


async def a02(ctx: Ctx) -> Result:
    room_id = ctx.state["roomId"]  # from A1
    front = await front_agent(ctx, "sam", room_id)
    ask = await post(ctx, "sam", room_id, ASKS)
    # 1. the Router's agent message carrying the task cards (not just the rows), 2. the SUB row of its first started task
    card = None
    for _ in range(ROUTER_REPLY_S // 2):
        card = next(
            (
                m
                for m in await messages(ctx, "sam", room_id)
                if (m.get("author") or {}).get("kind") == "agent"
                and m.get("kind") != "result"
                and m.get("messageSeq", 0) > ask["messageSeq"]
                and m.get(
                    "taskRefs"
                )  # the Router may fold a task into an existing one: fewer than 3 cards is allowed; taskCount in the evidence shows it
            ),
            None,
        )
        if card:
            break
        await ctx.sleep(2)
    first_started = next((r["taskId"] for r in (card or {}).get("taskRefs") or [] if r.get("role") == "started"), None)
    await ctx.wait_for(
        lambda rows: any(s["taskId"] == first_started for s in rows_of(rows, "subscription")) or None, SUB_S
    )
    rows = await ctx.tasks_rows(ctx.ws)
    tasks = tasks_from(rows, ask["messageId"])
    reply = [card] if card else []
    ask_trigger = ts.trigger_id("chat", ask["messageId"])
    trig = next((t for t in rows_of(rows, "trigger") if t["triggerId"] == ask_trigger), {})
    subs = {s["taskId"] for s in rows_of(rows, "subscription") if s.get("createdBy") == "router"}
    statuses = sorted(t["status"] for t in tasks)
    purchase = next((t["taskId"] for t in tasks if PURCHASE.search(t.get("title") or "")), None)
    # Set first: A5/A6/A7 and `answer_question` never touch this task's Questions, whatever happens below.
    ctx.state["purchaseTaskId"] = purchase
    asked = stopped = None
    if purchase:
        try:  # the paid step stops at an approval Question; it is never answered
            asked = await ctx.wait_for(lambda rows: question_row(rows, purchase, ("approval",)), PURCHASE_Q_S)
        finally:  # whatever happened above, the purchase task does not stay running
            stopped = await _stop(ctx, purchase)
    started_refs = [r for r in (reply[0]["taskRefs"] if reply else []) if r.get("role") == "started"]
    checks = {
        "one_router_reply_with_cards": len(reply) == 1 and len(started_refs) >= 2,
        "every_ask_started": len(tasks) >= 2 and all(s in STARTED for s in statuses),
        "purchase_waits_on_open_approval": asked is not None,
        "purchase_stopped_without_approving": stopped == 200,
        "fee_estimate_inline": len(reply) == 1 and bool(ESTIMATE.search(reply[0].get("text") or "")),
        "specialist_set": all(t.get("specialist") for t in tasks),
        "first_started_task_subscribed": first_started in subs,  # one owner per thread: only the first started task
        "trigger_respond_routed": trig.get("admission") in ("respond", "candidate")
        and bool(trig.get("routedTo")),  # unaddressed: Jev says candidate,
        "no_worker_authored_message": not worker_authored(
            await messages(ctx, "sam", room_id), (front or {}).get("id", "")
        ),
    }
    ctx.state["taskIds"] = [t["taskId"] for t in tasks if t["taskId"] != purchase]  # attempts, SUB, VM
    ctx.state["cardMessageId"] = reply[0]["messageId"] if reply else None
    ctx.state["firstStartedTaskId"] = first_started  # A13 expects a SUB row for this task only
    return Result(
        all(checks.values()),
        {"askMessageId": ask["messageId"], "statuses": statuses, "taskCount": len(tasks), **checks},
    )
