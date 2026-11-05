"""A12 (Router part) — private lane: Sam's DM reply lists his threads; Priya's canary never appears."""

from __future__ import annotations

import uuid

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ROUTER_REPLY_S, messages, post


async def a12_router(ctx: Ctx) -> Result:
    canary = f"CANARY-PRIYA-{uuid.uuid4()}"
    await post(ctx, "priya", ctx.state["priyaDmRoomId"], f"Remember this for me: {canary}")
    sam_dm = ctx.state["samDmRoomId"]
    ask = await post(ctx, "sam", sam_dm, "How is everything going?")
    reply = None
    for _ in range(ROUTER_REPLY_S * 2):  # Sam's Router reply, read through the messages API
        rows = [
            m
            for m in await messages(ctx, "sam", sam_dm)
            if (m.get("author") or {}).get("kind") == "agent" and m.get("messageSeq", 0) > ask.get("messageSeq", 0)
        ]
        if rows:
            reply = rows[-1]
            break
        await ctx.sleep(0.5)
    text = (reply or {}).get("text") or ""
    titles = [
        t.get("title", "")
        for t in await ctx.tasks_rows(ctx.ws)
        if t.get("type") == "task" and t.get("ownerUserId") == ctx.users["sam"]
    ]
    checks = {
        "sam_got_a_reply": reply is not None,
        "lists_sams_threads": any(w and w.lower().split()[0] in text.lower() for w in titles) if titles else True,
        "canary_absent_from_reply": canary not in text,
        "canary_absent_from_links": canary not in str((reply or {}).get("taskRefs") or ""),
    }
    return Result(all(checks.values()), {"canary": canary, "replyMessageId": (reply or {}).get("messageId"), **checks})
