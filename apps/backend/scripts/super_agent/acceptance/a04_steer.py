"""A4 — anyone can steer: Priya's reply in the card's thread reaches the task as `input`, 👀 ≤ 5 s, no new decision."""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ACK_S, ROUTER_REPLY_S, messages, post, rows_of

from core import tasks_schema as ts

ROUTE_S = ROUTER_REPLY_S  # an unaddressed thread reply may take the Jev route (a candidate), not the 5 s pass-through


async def a04(ctx: Ctx) -> Result:
    room_id, card = ctx.state["roomId"], ctx.state.get("cardMessageId")
    if not card:
        return Result(False, {"reason": "needs A2: no card message in ctx.state to reply under"})
    before = len(rows_of(await ctx.tasks_rows(ctx.ws), "task"))
    reply = await post(
        ctx, "priya", room_id, "Use the purple theme and add a dietary field to the signup.", thread_root=card
    )
    trigger_id = ts.trigger_id("chat", reply["messageId"])
    eyes = None
    for _ in range(ACK_S * 2):  # the 👀 ack is a reaction on Priya's reply, read through the messages API
        mine = [m for m in await messages(ctx, "priya", room_id) if m.get("messageId") == reply["messageId"]]
        if mine and "👀" in str(mine[0].get("reactions") or ""):
            eyes = mine[0]["reactions"]
            break
        await ctx.sleep(0.5)
    passed = await ctx.wait_for(
        lambda rows: next(
            (
                t
                for t in rows_of(rows, "trigger")
                if t["triggerId"] == trigger_id and (t.get("routedTo") or {}).get("taskId")
            ),
            None,
        ),
        ROUTE_S,
    )
    task_id = (passed or {}).get("routedTo", {}).get("taskId")
    after = rows_of(await ctx.tasks_rows(ctx.ws), "task")
    attempts = [
        a
        for a in rows_of(await ctx.tasks_rows(ctx.ws), "attempt")
        if a["taskId"] == task_id and trigger_id in (a.get("inputTriggerIds") or [])
    ]
    checks = {
        "posted_as_thread_reply": reply.get("threadRootMessageId") == card,
        # A steered input (live run) gets the 👀 and is never recorded on an attempt; a held one is (`begin_attempt`).
        "input_reached_task": task_id in ctx.state["taskIds"] and (bool(eyes) or bool(attempts)),
        "eyes_ack_within_5s": bool(eyes),
        "no_new_router_decision": len(after) == before,
    }
    return Result(all(checks.values()), {"replyMessageId": reply["messageId"], "taskId": task_id, **checks})
