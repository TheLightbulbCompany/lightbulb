"""A2 — ask for several things at once: one reply, two started, one suggested, the estimate inline (spec 09 §8.3).

A quick factual answer (the Stripe fee estimate) stays in the Router's reply line: no task, no card. Proposals are for
work that spends money: the domain purchase is PROPOSED (Suggested card), never started here."""

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
    rows_of,
    tasks_from,
    worker_authored,
)


SUB_S = 30
STARTED = ("queued", "working", "waiting", "idle", "resolved")  # the worker may already be past working
ESTIMATE = re.compile(r"[$%]\s*\d|\d\s*%|\d+\s*(?:dollars|cents)", re.I)  # a figure in the reply line


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
                and len(m.get("taskRefs") or []) >= 3
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
    checks = {
        "one_router_reply_with_three_cards": len(reply) == 1 and len(reply[0]["taskRefs"]) == 3,
        "two_started_one_proposed": len(tasks) == 3
        and statuses.count("proposed") == 1
        and all(s in STARTED or s == "proposed" for s in statuses),
        "fee_estimate_inline": len(reply) == 1 and bool(ESTIMATE.search(reply[0].get("text") or "")),
        "specialist_set": all(t.get("specialist") for t in tasks),
        "first_started_task_subscribed": first_started in subs,  # one owner per thread: only the first started task
        "trigger_respond_routed": trig.get("admission") in ("respond", "candidate")
        and bool(trig.get("routedTo")),  # unaddressed: Jev says candidate,
        "no_worker_authored_message": not worker_authored(
            await messages(ctx, "sam", room_id), (front or {}).get("id", "")
        ),
    }
    ctx.state["taskIds"] = [t["taskId"] for t in tasks]
    ctx.state["proposedTaskId"] = next((t["taskId"] for t in tasks if t["status"] == "proposed"), None)
    ctx.state["cardMessageId"] = reply[0]["messageId"] if reply else None
    ctx.state["firstStartedTaskId"] = first_started  # A13 expects a SUB row for this task only
    return Result(
        all(checks.values()),
        {"askMessageId": ask["messageId"], "statuses": statuses, "taskCount": len(tasks), **checks},
    )
