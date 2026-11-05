"""A1 — one shared room, one coordinator (spec 09 §8.3; mockup step 1)."""

from __future__ import annotations

import time

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
    # Addressed on purpose: an unaddressed greeting goes to Jev (admission slow path), which on dev ignored it (no
    # trigger row, no reply in 180 s, 5 Nov). The mention takes the fast path, so A1 tests the Router, not Jev.
    first = await post(
        ctx, "sam", room["roomId"], "Hi @lightbulb, we're launching the workshop in March.", mentions=[front["id"]]
    )
    started = time.monotonic()
    trigger_id = ts.trigger_id("chat", first["messageId"])
    # Admission wrote the Trigger row (a plain reply has no `routedTo`; that is only set when a task started).
    routed = await ctx.wait_for(
        lambda rows: any(t["triggerId"] == trigger_id for t in rows_of(rows, "trigger")) or None,
        ROUTER_REPLY_S,
    )
    routed_s = round(time.monotonic() - started, 1)
    replies: list[dict] = []
    while time.monotonic() - started < ROUTER_REPLY_S:  # the reply post can trail the routing row
        replies = [
            m for m in await messages(ctx, "sam", room["roomId"]) if (m.get("author") or {}).get("kind") == "agent"
        ]
        if replies:
            break
        await ctx.sleep(2)
    await ctx.sleep(5)  # a second reply, if the Router wrongly sends one, lands shortly after the first
    replies = [m for m in await messages(ctx, "sam", room["roomId"]) if (m.get("author") or {}).get("kind") == "agent"]
    checks = {
        "one_agent_member_lightbulb": front is not None and front.get("mentionHandle") == "lightbulb",
        "first_message_admitted": bool(routed),
        "one_router_reply": len(replies) == 1,
        "no_double_reply": len(replies) <= 1,
    }
    return Result(
        all(checks.values()),
        {
            "roomId": room["roomId"],
            "front": front,
            "routedSeconds": routed_s,
            "repliedSeconds": round(time.monotonic() - started, 1),
            "triggerId": trigger_id,
            **checks,
        },
    )
