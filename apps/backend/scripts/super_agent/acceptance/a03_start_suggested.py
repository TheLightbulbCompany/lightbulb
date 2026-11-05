"""A3 — start the suggested thread: proposed → queued → working ≤ 15 s; task.changed ≤ 3 s."""

from __future__ import annotations

import asyncio
import time

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import EVENT_S, START_S, question_row, rows_of, task_detail

PURCHASE_Q_S = 180


async def _stop(ctx: Ctx, task_id: str) -> int | None:
    """Best-effort Stop of the purchase task; its HTTP status, or None when even that failed."""
    try:
        version = (await task_detail(ctx, "sam", task_id))["task"]["version"]
        return (await ctx.api("sam").post(f"/api/v1/tasks/{task_id}/stop", json={"version": version})).status_code
    except Exception:  # noqa: BLE001 -- the original error (if any) is the one to surface
        return None


async def a03(ctx: Ctx) -> Result:
    task_id = ctx.state.get("proposedTaskId")
    if task_id is None:  # A2 proposes the domain purchase; without it there is nothing to start
        return Result(False, {"reason": "no proposed task to start (A2 did not propose the domain purchase)"})
    ctx.state["purchaseTaskId"] = task_id  # A5/A6 never touch this task's Questions
    detail = await task_detail(ctx, "sam", task_id)
    events: list[dict] = []

    async def listen() -> None:
        async for event in ctx.appsync(ctx.ws):
            if event.get("type") == "task.changed" and event["payload"]["taskId"] == task_id:
                events.append({"at": time.monotonic(), **event["payload"]})

    listener = asyncio.create_task(listen())
    pressed = time.monotonic()
    working = asked = None
    try:
        r = await ctx.api("sam").post(f"/api/v1/tasks/{task_id}/start", json={"version": detail["task"]["version"]})
        r.raise_for_status()
        working = await ctx.wait_for(
            lambda rows: next(
                (t for t in rows_of(rows, "task") if t["taskId"] == task_id and t["status"] in ("working", "waiting")),
                None,
            ),
            START_S,
        )
        await asyncio.sleep(EVENT_S)
        # The purchase must stop at an approval Question. It is never answered.
        asked = await ctx.wait_for(lambda rows: question_row(rows, task_id, ("approval",)), PURCHASE_Q_S)
    finally:  # whatever happened above, the purchase task does not stay running
        listener.cancel()
        stopped = await _stop(ctx, task_id)
    first_event = min((e["at"] for e in events), default=None)
    checks = {
        "working_within_15s": working is not None,
        "task_changed_within_3s": first_event is not None and first_event - pressed <= EVENT_S,
        "purchase_waits_on_open_approval": asked is not None,
        "stopped_without_approving": stopped == 200,
    }
    return Result(all(checks.values()), {"taskId": task_id, "events": len(events), **checks})
