"""A9 — Ready for review -> Landing -> Idle (spec 09 §8.3; Q Task 11 step 3), clean run.

Takes the task from A2 whose Effect is proposed behind an open ``review`` Question (the email), checks every
recipient is a ``+clerk_test@thelightbulb.company`` address (otherwise fails WITHOUT pressing), presses Send as
Sam and watches the Effect and the shown group. Not asserted here, by hand: the two fault runs
(``kill_after_effect_intent`` / ``kill_after_tool_before_done`` are env on the dev service; ``--faults`` only
records them), "exactly one email" in the outbound log, and the Apps publish gate on an unpublishable preview.
"""

from __future__ import annotations

import re

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import answer_question, question_row, rows_of, task_detail, task_questions

TEST_ADDRESS = re.compile(r"^[^@\s]+\+clerk_test@thelightbulb\.company$", re.I)
REVIEW_S = 300
LAND_S = 120


def _effects(rows: list[dict], task_id: str) -> list[dict]:
    return [e for e in rows_of(rows, "effect") if e.get("taskId") == task_id]


async def a09(ctx: Ctx) -> Result:
    task_ids = list(ctx.state.get("taskIds") or [])
    if not task_ids:
        return Result(False, {"reason": "needs A2: no task ids in ctx.state"})
    found = await ctx.wait_for(
        lambda rows: next((q for t in task_ids if (q := question_row(rows, t, ("review",)))), None), REVIEW_S
    )
    if found is None:
        return Result(False, {"reason": "no open review Question on any A2 task", "taskIds": task_ids})
    task_id, qid = found["taskId"], found["qid"]
    mails = [e for e in _effects(await ctx.tasks_rows(ctx.ws), task_id) if e.get("kind") == "mail.send"]
    recipients = [
        a.strip() for e in mails for a in re.split(r"[,;]", str((e.get("operands") or {}).get("to") or "")) if a.strip()
    ]
    if not mails or not recipients or not all(TEST_ADDRESS.match(a) for a in recipients):
        # A real send must never come from a gate run: refuse WITHOUT pressing.
        return Result(
            False,
            {
                "reason": "refusing to press Send: recipients are not all +clerk_test@thelightbulb.company",
                "taskId": task_id,
                "recipients": recipients,
            },
        )
    before = await task_detail(ctx, "sam", task_id)
    view = next(q for q in await task_questions(ctx, "sam", task_id) if q["id"] == qid)
    press = await answer_question(
        ctx, "sam", task_id, qid, next(o["id"] for o in view["options"] if o["style"] == "primary"), key="a09-sam"
    )
    groups: list[str] = []
    for _ in range(LAND_S * 4):  # 0.25 s: Landing is brief when the executor is fast
        group = (await task_detail(ctx, "sam", task_id))["task"].get("shownGroup")  # the summary holds it
        if group and (not groups or groups[-1] != group):
            groups.append(group)
        effects = _effects(await ctx.tasks_rows(ctx.ws), task_id)
        if (
            effects
            and all(e["state"] in ("done", "unknown") for e in effects)
            and groups
            and groups[-1] in ("idle", "resolved")
        ):
            break
        await ctx.sleep(0.25)
    effects = _effects(await ctx.tasks_rows(ctx.ws), task_id)
    sent = [e for e in effects if e.get("kind") == "mail.send"]
    checks = {
        "was_ready_for_review": before["task"].get("shownGroup") == "ready_for_review",
        "send_press_accepted": press.status_code == 200,
        "one_effect_ran_once": len(sent) == 1 and sent[0]["state"] == "done" and bool(sent[0].get("resultRef")),
        "intent_recorded_before_done": bool(sent) and bool(sent[0].get("doorUsedAt")),
        "ended_idle": bool(groups) and groups[-1] in ("idle", "resolved"),
    }
    return Result(
        all(checks.values()),
        {
            "taskId": task_id,
            "recipients": recipients,
            "groups": groups,  # Landing is shown when the executor outlasts the poll; evidence, not a check
            "effects": [{"idemKey": e["idemKey"], "kind": e.get("kind"), "state": e["state"]} for e in effects],
            "byHand": "outbound log has one email; fault runs; Apps publish gate",
            **checks,
        },
    )
