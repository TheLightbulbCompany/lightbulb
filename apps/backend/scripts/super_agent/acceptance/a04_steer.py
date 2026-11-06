"""A4 — anyone can steer: Priya's reply in a working task's card thread reaches the task as `input`, 👀 ≤ 5 s,
no new decision.

Since #4876 the A2 signup task hands the app to Apps and ends in ~30 s, so a reply under A2's card is held (no 👀).
A4 starts its own long-running task (research: no money, no email, no app) and Priya replies once its row is
`working`: a live steer is what earns the 👀. The task is stopped afterwards (the steer is the test, not the research).
"""

from __future__ import annotations

from scripts.super_agent.acceptance.a02_many_asks import _stop
from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ACK_S, ROUTER_REPLY_S, messages, post, rows_of

from core import tasks_schema as ts

ROUTE_S = ROUTER_REPLY_S  # an unaddressed thread reply may take the Jev route (a candidate), not the 5 s pass-through
STEER_ASK = (
    "Research five venues in Brooklyn that could host a 40-person evening workshop and write a comparison of "
    "price, capacity and transit for each. Don't contact or book anything."
)


async def _started_card(ctx: Ctx, room_id: str, ask: dict) -> tuple[dict | None, str | None]:
    """The Router's card for ``ask`` and the task it started (the card thread's subscribed owner)."""
    for _ in range(ROUTER_REPLY_S // 2):
        for m in await messages(ctx, "sam", room_id):
            if (m.get("author") or {}).get("kind") != "agent" or m.get("messageSeq", 0) <= ask["messageSeq"]:
                continue
            started = next((r["taskId"] for r in m.get("taskRefs") or [] if r.get("role") == "started"), None)
            if started:
                return m, started
        await ctx.sleep(2)
    return None, None


def _working(task_id: str):
    return lambda rows: next(
        (t for t in rows_of(rows, "task") if t["taskId"] == task_id and t["status"] == "working"), None
    )


async def a04(ctx: Ctx) -> Result:
    room_id = ctx.state.get("roomId")
    if not room_id:
        return Result(False, {"reason": "needs A1: no roomId in ctx.state"})
    ask = await post(ctx, "sam", room_id, STEER_ASK)
    card, steer_task = await _started_card(ctx, room_id, ask)
    if not card:
        return Result(False, {"reason": "the Router started no task for the steer ask", "askId": ask["messageId"]})
    try:  # whatever raises below, the research task does not stay running (`_stop` itself never raises)
        result = await _steer(ctx, room_id, card["messageId"], steer_task)
    finally:
        stopped = await _stop(ctx, steer_task)
    result.evidence["stopped"] = stopped
    return result


async def _steer(ctx: Ctx, room_id: str, card_id: str, steer_task: str) -> Result:
    if not await ctx.wait_for(_working(steer_task), ROUTER_REPLY_S):
        return Result(False, {"reason": "steer task never reached working", "taskId": steer_task})
    before = len(rows_of(await ctx.tasks_rows(ctx.ws), "task"))
    reply = await post(
        ctx, "priya", room_id, "Only venues within 10 minutes of a subway stop, please.", thread_root=card_id
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
    rows = await ctx.tasks_rows(ctx.ws)
    attempts = [
        a for a in rows_of(rows, "attempt") if a["taskId"] == task_id and trigger_id in (a.get("inputTriggerIds") or [])
    ]
    checks = {
        "posted_as_thread_reply": reply.get("threadRootMessageId") == card_id,
        # A steered input (live run) gets the 👀 and is never recorded on an attempt; a held one is (`begin_attempt`).
        "input_reached_task": task_id == steer_task and (bool(eyes) or bool(attempts)),
        "eyes_ack_within_5s": bool(eyes),
        "no_new_router_decision": len(rows_of(rows, "task")) == before,
    }
    return Result(all(checks.values()), {"replyMessageId": reply["messageId"], "taskId": task_id, **checks})
