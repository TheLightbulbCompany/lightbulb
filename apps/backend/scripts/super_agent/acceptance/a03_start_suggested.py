"""A3 — start the suggested thread: proposed → queued → working ≤ 15 s; task.changed ≤ 3 s."""

from __future__ import annotations

import asyncio
import time

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import EVENT_S, START_S, rows_of, task_detail


async def a03(ctx: Ctx) -> Result:
    task_id = ctx.state["proposedTaskId"]
    detail = await task_detail(ctx, "sam", task_id)
    events: list[dict] = []

    async def listen() -> None:
        async for event in ctx.appsync(ctx.ws):
            if event.get("type") == "task.changed" and event["payload"]["taskId"] == task_id:
                events.append({"at": time.monotonic(), **event["payload"]})

    listener = asyncio.create_task(listen())
    pressed = time.monotonic()
    r = await ctx.api("sam").post(f"/api/v1/tasks/{task_id}/start", json={"version": detail["task"]["version"]})
    r.raise_for_status()
    working = await ctx.wait_for(
        lambda rows: next(
            (t for t in rows_of(rows, "task") if t["taskId"] == task_id and t["status"] == "working"), None
        ),
        START_S,
    )
    await asyncio.sleep(EVENT_S)
    listener.cancel()
    first_event = min((e["at"] for e in events), default=None)
    checks = {
        "working_within_15s": working is not None,
        "task_changed_within_3s": first_event is not None and first_event - pressed <= EVENT_S,
    }
    return Result(all(checks.values()), {"taskId": task_id, "events": len(events), **checks})
