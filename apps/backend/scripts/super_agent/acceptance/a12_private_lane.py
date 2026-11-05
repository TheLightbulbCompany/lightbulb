"""A12 (memory part) — private lane (spec 09 §8.3; M Task 10).

Seeds ``CANARY-PRIYA-<uuid>`` in Priya's DM with @lightbulb and waits for it to be extracted; Priya's recall must
return it. Sam asks "How is everything going?" in his DM: the Router's reply must not contain it, nor Sam's
memory-API recall (shared + his own private scope) or its ``links[]``. The Router's own internal ``recall`` log is
not readable from here, so Sam's API recall stands in for it.
"""

from __future__ import annotations

import uuid

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ROUTER_REPLY_S, agent_posts, messages, post

EXTRACT_S = 180
RECALL = "/api/v1/memory/recall"


async def _recall(ctx: Ctx, user: str, canary: str) -> dict:
    r = await ctx.api(user).post(RECALL, json={"question": f"What is {canary}? Remember this for me."})
    r.raise_for_status()
    return r.json()


async def a12(ctx: Ctx) -> Result:
    canary = f"CANARY-PRIYA-{uuid.uuid4()}"
    seed = await post(ctx, "priya", ctx.state["priyaDmRoomId"], f"Please remember this secret code for me: {canary}")
    page = None
    for _ in range(EXTRACT_S // 5):  # extraction lags the message; poll Priya's own recall
        page = await _recall(ctx, "priya", canary)
        if canary in str(page):
            break
        await ctx.sleep(5)
    sam = await _recall(ctx, "sam", canary)
    sam_dm = ctx.state["samDmRoomId"]
    ask = await post(ctx, "sam", sam_dm, "How is everything going?")
    reply = None
    for _ in range(ROUTER_REPLY_S * 2):
        got = [m for m in agent_posts(await messages(ctx, "sam", sam_dm)) if m.get("messageSeq", 0) > ask["messageSeq"]]
        if got:
            reply = got[-1]
            break
        await ctx.sleep(0.5)
    checks = {
        "priya_can_recall_her_canary": canary in str(page),
        "sam_got_a_reply": reply is not None,
        "canary_absent_from_sams_reply": canary not in str(reply),
        "canary_absent_from_sams_recall": canary not in str(sam),
        "canary_absent_from_sams_links": canary not in str(sam.get("links") or []),
    }
    return Result(
        all(checks.values()),
        {
            "canary": canary,
            "seedMessageId": seed["messageId"],
            "samReplyMessageId": (reply or {}).get("messageId"),
            "samLinkIds": [link.get("id") for link in sam.get("links") or []],
            **checks,
        },
    )
