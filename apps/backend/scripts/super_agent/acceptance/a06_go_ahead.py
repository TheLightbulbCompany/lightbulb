"""A6 — "go ahead" is not an approval (spec 09 §8.3; Q Task 11 step 2). Needs A5's open approval Question."""

from __future__ import annotations

from core.services.questions import MONEY_SUBKINDS
from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import (
    ACK_S,
    ROUTER_REPLY_S,
    agent_posts,
    answer_question,
    messages,
    post,
    rows_of,
    task_questions,
)


async def a06(ctx: Ctx) -> Result:
    if "approvalQid" not in ctx.state:
        return Result(False, {"reason": "needs A5: no open approval Question in ctx.state (run A5 first)"})
    task_id, qid = ctx.state["approvalTaskId"], ctx.state["approvalQid"]
    if task_id == ctx.state.get("purchaseTaskId"):
        return Result(False, {"reason": "refusing: A6 never approves the purchase task's Question"})
    room_id = ctx.state["roomId"]
    q = next(q for q in await task_questions(ctx, "priya", task_id) if q["id"] == qid)
    if q.get("subkind") in MONEY_SUBKINDS:
        return Result(False, {"reason": "refusing to approve a money Question", "subkind": q.get("subkind")})
    qrow = next(
        r for r in rows_of(await ctx.tasks_rows(ctx.ws), "question") if r["taskId"] == task_id and r["qid"] == qid
    )
    card = qrow.get("relay") or {}
    sam_name = (q["answerers"][0] or {}).get("name") or ""
    go = await post(ctx, "priya", card.get("roomId") or room_id, "go ahead", thread_root=card.get("messageId"))
    reply = None
    for _ in range(ROUTER_REPLY_S * 2):
        got = [
            m for m in agent_posts(await messages(ctx, "priya", room_id)) if m.get("messageSeq", 0) > go["messageSeq"]
        ]
        if got:
            reply = got[-1]
            break
        await ctx.sleep(0.5)
    rows = await ctx.tasks_rows(ctx.ws)
    still_open = next(r for r in rows_of(rows, "question") if r["taskId"] == task_id and r["qid"] == qid)
    task = next(t for t in rows_of(rows, "task") if t["taskId"] == task_id)
    option = next(o["id"] for o in q["options"] if o["style"] == "primary")
    pressed = await answer_question(ctx, "sam", task_id, qid, option, key="a06-sam")
    left = await ctx.wait_for(
        lambda rs: next((t for t in rows_of(rs, "task") if t["taskId"] == task_id and t["status"] != "waiting"), None),
        ACK_S,
    )
    answered = next(r for r in rows_of(await ctx.tasks_rows(ctx.ws), "question") if r["qid"] == qid)
    checks = {
        "router_replied_to_go_ahead": reply is not None,
        "reply_names_sam": bool(sam_name) and sam_name.lower() in ((reply or {}).get("text") or "").lower(),
        "question_still_open_after_priya": still_open["state"] == "open",
        "task_still_waiting_after_priya": task["status"] == "waiting",
        "sam_press_accepted": pressed.status_code == 200,
        "answered_by_sam": answered.get("answeredBy") == ctx.users["sam"],
        "workflow_left_wait_within_5s": left is not None,
    }
    return Result(all(checks.values()), {"taskId": task_id, "qid": qid, "goAheadMessageId": go["messageId"], **checks})
