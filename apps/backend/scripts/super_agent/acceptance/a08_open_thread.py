"""A8 — open a thread: the task page's data equals the rows; the trace loads on demand."""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import rows_of, task_detail


async def a08(ctx: Ctx) -> Result:
    task_id = ctx.state["taskIds"][0]
    detail = await task_detail(ctx, "sam", task_id)
    rows = await ctx.tasks_rows(ctx.ws)
    row = next(t for t in rows_of(rows, "task") if t["taskId"] == task_id)
    attempts = [a for a in rows_of(rows, "attempt") if a["taskId"] == task_id]
    trace = await ctx.api("sam").get(f"/api/v1/tasks/{task_id}/trace", params={"attempt": len(attempts) or 1})
    usage = sum(int((a.get("usage") or {}).get("inputTokens") or 0) for a in attempts)
    checks = {
        "status_and_version_match_rows": detail["task"]["status"] == row["status"]
        and detail["task"]["version"] == row["version"],
        "attempts_match_rows": len(detail.get("attempts", [])) == len(attempts),
        "trace_loads": trace.status_code == 200 and "recentTools" in trace.json(),
        "cost_is_attempt_usage": (detail["task"].get("usage") or {}).get("inputTokens", usage) == usage,
    }
    return Result(all(checks.values()), {"taskId": task_id, **checks})
