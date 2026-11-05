"""A5 — an approval only the requester can give (spec 09 §8.3; Q Task 11 step 1).

Reads the open ``approval`` Question an app task raised for the DB + Stripe ask (A2 leaves the task ids in
``ctx.state["taskIds"]``; the signup-page task is the app task). Push-to-Sam-only and the UI wording are by hand
(no push log API), noted in the evidence.
"""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import answer_question, question_row, rows_of, task_questions

ASK_S = 600


async def a05(ctx: Ctx) -> Result:
    # Never the purchase task A3 started: nothing here may approve a purchase.
    task_ids = [t for t in ctx.state.get("taskIds") or [] if t != ctx.state.get("purchaseTaskId")]
    found = await ctx.wait_for(
        lambda rows: next((q for t in task_ids if (q := question_row(rows, t, ("approval",)))), None), ASK_S
    )
    if found is None:
        return Result(False, {"reason": "no open approval Question on any A2 task", "taskIds": task_ids})
    task_id, qid = found["taskId"], found["qid"]
    ctx.state.update(approvalTaskId=task_id, approvalQid=qid)
    rows = await ctx.tasks_rows(ctx.ws)
    task = next(t for t in rows_of(rows, "task") if t["taskId"] == task_id)
    needs = {n["userId"] for n in rows_of(rows, "needs") if n.get("taskId") == task_id and n.get("qid") == qid}
    sam_view = next(q for q in await task_questions(ctx, "sam", task_id) if q["id"] == qid)
    priya_view = next(q for q in await task_questions(ctx, "priya", task_id) if q["id"] == qid)
    sam_name = (priya_view["answerers"][0] or {}).get("name")
    refused = await answer_question(ctx, "priya", task_id, qid, sam_view["options"][0]["id"], key="a05-priya")
    after = next(q for q in await task_questions(ctx, "sam", task_id) if q["id"] == qid)
    checks = {
        "approval_question_open": found["state"] == "open" and found["kind"] == "approval",
        "task_waiting": task["status"] == "waiting",
        "needs_row_for_sam_only": needs == {ctx.users["sam"]},
        "sam_can_answer": sam_view["canAnswer"] is True and sam_view["waitingLabel"] == "Waiting on you",
        "priya_cannot_answer": priya_view["canAnswer"] is False
        and priya_view["waitingLabel"] == f"Waiting on {sam_name}",
        "priya_answer_is_403": refused.status_code == 403,
        "still_open_after_priya": after["state"] == "open",
    }
    return Result(
        all(checks.values()),
        {
            "taskId": task_id,
            "qid": qid,
            "needs": sorted(needs),
            "priyaLabel": priya_view["waitingLabel"],
            "byHand": "push log has Sam only; card, overview and Activity wording (UI)",
            **checks,
        },
    )
