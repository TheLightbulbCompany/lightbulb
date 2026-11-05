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


STARTED = ("queued", "working", "waiting", "idle", "resolved")  # the worker may already be past working
ESTIMATE = re.compile(r"[$%]\s*\d|\d\s*%|\d+\s*(?:dollars|cents)", re.I)  # a figure in the reply line


def _three(tasks: list[dict]) -> list[dict] | None:
    return tasks if len(tasks) >= 3 else None


async def a02(ctx: Ctx) -> Result:
    room_id = ctx.state["roomId"]  # from A1
    front = await front_agent(ctx, "sam", room_id)
    ask = await post(ctx, "sam", room_id, ASKS)
    tasks = await ctx.wait_for(lambda rows: _three(tasks_from(rows, ask["messageId"])), ROUTER_REPLY_S)
    rows = await ctx.tasks_rows(ctx.ws)
    tasks = tasks or tasks_from(rows, ask["messageId"])  # fewer than three: report what the Router did make
    reply = [
        m
        for m in await messages(ctx, "sam", room_id)
        if (m.get("author") or {}).get("kind") == "agent" and m.get("taskRefs")
    ]
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
        "subscribed": any(t["taskId"] in subs for t in tasks),
        "trigger_respond_routed": trig.get("admission") == "respond" and bool(trig.get("routedTo")),
        "no_worker_authored_message": not worker_authored(
            await messages(ctx, "sam", room_id), (front or {}).get("id", "")
        ),
    }
    ctx.state["taskIds"] = [t["taskId"] for t in tasks]
    ctx.state["proposedTaskId"] = next((t["taskId"] for t in tasks if t["status"] == "proposed"), None)
    ctx.state["cardMessageId"] = reply[0]["messageId"] if reply else None
    return Result(
        all(checks.values()),
        {"askMessageId": ask["messageId"], "statuses": statuses, "taskCount": len(tasks), **checks},
    )
