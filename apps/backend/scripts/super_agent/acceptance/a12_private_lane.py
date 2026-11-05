"""A12 (memory part) — private lane (spec 09 §8.3; M Task 10).

Seeds ``CANARY-PRIYA-<uuid>`` in Priya's DM with @lightbulb, waits for it to be extracted, then Sam asks the
memory API (the same recall the Router uses, as Sam: shared + his own private scope) and must not get it; Priya
still can. Not readable from here: the Router's own ``recall`` logs, so Sam's API recall stands in for them.
"""

from __future__ import annotations

import uuid

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import post

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
    checks = {
        "priya_can_recall_her_canary": canary in str(page),
        "canary_absent_from_sams_recall": canary not in str(sam),
        "canary_absent_from_sams_links": canary not in str(sam.get("links") or []),
    }
    return Result(
        all(checks.values()),
        {
            "canary": canary,
            "seedMessageId": seed["messageId"],
            "samLinkIds": [link.get("id") for link in sam.get("links") or []],
            **checks,
        },
    )
