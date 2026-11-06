"""A7 — results through one voice: only Lightbulb posts, labelled "Lightbulb · <specialist>"; zero Worker posts."""

from __future__ import annotations

from core import tasks_schema as ts
from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import front_agent, poll_messages, rows_of, worker_authored

RESULT_S = 600


async def a07(ctx: Ctx) -> Result:
    room_id = ctx.state["roomId"]
    front = await front_agent(ctx, "sam", room_id)
    # The Stripe estimate is answered inline now (no Finance task): take the first started A2 task that finishes.
    candidates = [t for t in ctx.state["taskIds"] if t != ctx.state.get("purchaseTaskId")]
    done = await ctx.wait_for(
        lambda rows: next(
            (t for t in rows_of(rows, "task") if t["taskId"] in candidates and t["status"] in ("idle", "resolved")),
            None,
        ),
        RESULT_S,
    )
    finance = done  # the finished task; its specialist labels the result
    task_id = (finance or {}).get("taskId")

    def results(rows: list[dict]) -> list[dict]:  # the result line in the task's room (`activities.end_attempt`)
        return [m for m in rows if m.get("kind") == "result"]

    # A Router task's main chat card shows its result in place (dev walk 10): the result post is in the task room.
    # The task goes idle before `end_attempt` posts the result (run 4: read once, between the two).
    task_room = ts.task_room_id(task_id) if task_id else room_id
    result = results(await poll_messages(ctx, "sam", task_room, results, limit=100))
    room_rows = await poll_messages(ctx, "sam", room_id, lambda rows: rows, limit=100)
    checks = {
        "a_started_task_finished": done is not None,
        "result_posted_by_lightbulb": bool(result)
        and all(m["author"].get("id") == (front or {}).get("id") for m in result),
        "labelled_specialist": bool(result)
        and all(m.get("specialist") == (finance or {}).get("specialist") for m in result),
        "zero_worker_authored_messages": not worker_authored(room_rows, (front or {}).get("id", "")),
    }
    return Result(all(checks.values()), {"taskId": task_id, "results": len(result), **checks})
