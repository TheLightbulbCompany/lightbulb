"""A2 — ask for several things at once: one reply, two started, one suggested (spec 09 §8.3; G2, G18)."""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import (
    ROUTER_REPLY_S,
    THREE_ASKS,
    front_agent,
    messages,
    post,
    rows_of,
    tasks_from,
    worker_authored,
)


def _three(tasks: list[dict]) -> list[dict] | None:
    return tasks if len(tasks) >= 3 else None


async def a02(ctx: Ctx) -> Result:
    room_id = ctx.state["roomId"]  # from A1
    front = await front_agent(ctx, "sam", room_id)
    ask = await post(ctx, "sam", room_id, THREE_ASKS)
    tasks = await ctx.wait_for(lambda rows: _three(tasks_from(rows, ask["messageId"])), ROUTER_REPLY_S)
    rows = await ctx.tasks_rows(ctx.ws)
    reply = [
        m
        for m in await messages(ctx, "sam", room_id)
        if (m.get("author") or {}).get("kind") == "agent" and m.get("taskRefs")
    ]
    trig = next((t for t in rows_of(rows, "trigger") if t["triggerId"] == tasks[0]["triggerId"]), {})
    subs = {s["taskId"] for s in rows_of(rows, "subscription") if s.get("createdBy") == "router"}
    statuses = sorted(t["status"] for t in tasks)
    checks = {
        "one_router_reply_with_three_cards": len(reply) == 1 and len(reply[0]["taskRefs"]) == 3,
        "two_started_one_proposed": statuses.count("proposed") == 1
        and all(s in ("queued", "working", "proposed") for s in statuses),
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
    return Result(all(checks.values()), {"askMessageId": ask["messageId"], "statuses": statuses, **checks})
