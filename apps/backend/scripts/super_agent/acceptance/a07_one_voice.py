"""A7 — results through one voice: only Lightbulb posts, labelled "Lightbulb · Finance"; zero Worker posts."""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import front_agent, messages, rows_of, worker_authored

RESULT_S = 600


async def a07(ctx: Ctx) -> Result:
    room_id = ctx.state["roomId"]
    front = await front_agent(ctx, "sam", room_id)
    finance = next(
        (
            t
            for t in rows_of(await ctx.tasks_rows(ctx.ws), "task")
            if t["taskId"] in ctx.state["taskIds"] and "stripe" in (t.get("title") or "").lower()
        ),
        None,
    )
    task_id = (finance or {}).get("taskId")
    done = await ctx.wait_for(
        lambda rows: next(
            (t for t in rows_of(rows, "task") if t["taskId"] == task_id and t["status"] in ("idle", "resolved")), None
        ),
        RESULT_S,
    )
    room_rows = await messages(ctx, "sam", room_id, limit=100)
    result = [
        m
        for m in room_rows
        if m.get("kind") == "result" and any(r.get("taskId") == task_id for r in m.get("taskRefs") or [])
    ]
    checks = {
        "finance_task_finished": done is not None,
        "result_posted_by_lightbulb": bool(result)
        and all(m["author"].get("id") == (front or {}).get("id") for m in result),
        "labelled_specialist": bool(result)
        and all(m.get("specialist") == (finance or {}).get("specialist") for m in result),
        "zero_worker_authored_messages": not worker_authored(room_rows, (front or {}).get("id", "")),
    }
    return Result(all(checks.values()), {"taskId": task_id, "results": len(result), **checks})
