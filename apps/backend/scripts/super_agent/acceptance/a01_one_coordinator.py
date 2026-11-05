"""A1 — one shared room, one coordinator (spec 09 §8.3; mockup step 1)."""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ROUTER_REPLY_S, create_room, front_agent, messages, post, rows_of

from core import tasks_schema as ts


async def a01(ctx: Ctx) -> Result:
    room = await create_room(
        ctx,
        "sam",
        "workshop-launch",
        [{"kind": "human", "id": ctx.users["priya"], "role": "member", "listenMode": None}],
    )
    ctx.state["roomId"] = room["roomId"]  # A2, A4, A7 post into this room
    front = await front_agent(ctx, "sam", room["roomId"])
    first = await post(ctx, "sam", room["roomId"], "Hi Lightbulb, we're launching the workshop in March.")
    trigger_id = ts.trigger_id("chat", first["messageId"])
    # The Router (`router:{ws}`) is running when the admitted trigger is routed and answered.
    routed = await ctx.wait_for(
        lambda rows: any(t["triggerId"] == trigger_id and t.get("routedTo") for t in rows_of(rows, "trigger")) or None,
        ROUTER_REPLY_S,
    )
    replies = [m for m in await messages(ctx, "sam", room["roomId"]) if (m.get("author") or {}).get("kind") == "agent"]
    checks = {
        "one_agent_member_lightbulb": front is not None and front.get("mentionHandle") == "lightbulb",
        "router_routed_the_first_message": bool(routed),
        "one_router_reply": len(replies) == 1,
        "no_double_reply": len(replies) <= 1,
    }
    return Result(all(checks.values()), {"roomId": room["roomId"], "front": front, "triggerId": trigger_id, **checks})
