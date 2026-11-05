"""A11 — recurring work (spec 09 §8.3; A Task 13). Sam, in the A1 room. Steps 1-7 of the plan, in order.

Waits are real (a 2-minute one-off, then a 3-minute pause check); the Monday-9 behaviour itself is the
time-skipping test's. Manual: the card/History UI, the Stripe webhook resend and the Sendblue text.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.super_agent.acceptance.ctx import Ctx, Result
from scripts.super_agent.acceptance.o_lib import ROUTER_REPLY_S, messages, post, rows_of

ASK = "every Monday at 9am post signups here"
FIRE_S = 180
SCHEDULES = "/api/v1/automations"


def _schedules(rows: list[dict]) -> list[dict]:
    return rows_of(rows, "schedule")


def _cron_in(minutes: int, tz: str) -> str:
    when = datetime.now(ZoneInfo(tz)).timestamp() + minutes * 60
    t = datetime.fromtimestamp(when, ZoneInfo(tz))
    return f"{t.minute} {t.hour} {t.day} {t.month} *"


async def a11(ctx: Ctx) -> Result:
    api, room_id = ctx.api("sam"), ctx.state["roomId"]
    known = {s["scheduleId"] for s in _schedules(await ctx.tasks_rows(ctx.ws))}
    ask = await post(ctx, "sam", room_id, ASK)
    draft = await ctx.wait_for(
        lambda rows: next((s for s in _schedules(rows) if s["scheduleId"] not in known), None), ROUTER_REPLY_S
    )
    if draft is None:
        return Result(False, {"reason": "no SCHED# row after the ask", "askMessageId": ask["messageId"]})
    sid, tz = draft["scheduleId"], draft["tz"]
    posts = [m for m in await messages(ctx, "sam", room_id) if m.get("messageSeq", 0) > ask["messageSeq"]]
    confirm = next((m for m in posts if m.get("kind") == "automation_confirm"), None)
    router_text = " ".join(m.get("text") or "" for m in posts if (m.get("author") or {}).get("kind") == "agent")
    await post(ctx, "sam", room_id, "yes")
    await ctx.sleep(2)
    still_draft = (
        next(s for s in _schedules(await ctx.tasks_rows(ctx.ws)) if s["scheduleId"] == sid)["status"] == "draft"
    )

    activated = (await api.post(f"{SCHEDULES}/{sid}/activate")).raise_for_status().json()
    first_run = next(iter(activated["nextRuns"]), None)
    monday_nine = (
        bool(first_run)
        and (t := datetime.fromisoformat(first_run).astimezone(ZoneInfo(tz))).weekday() == 0
        and (t.hour, t.minute) == (9, 0)
    )

    await api.post(f"{SCHEDULES}/{sid}/run")
    run1 = await ctx.wait_for(
        lambda rows: next((t for t in rows_of(rows, "task") if t.get("policyRef") == f"SCHED#{sid}"), None), 60
    )
    second_now = await api.post(f"{SCHEDULES}/{sid}/run")

    view = (await api.get(f"{SCHEDULES}/{sid}")).raise_for_status().json()
    one_off = {"cron": [_cron_in(2, tz)], "tz": tz, "remainingRuns": 1}
    (await api.patch(f"{SCHEDULES}/{sid}", json={"schedule": one_off, "version": view["version"]})).raise_for_status()
    first_id = (run1 or {}).get("taskId")
    run2 = await ctx.wait_for(
        lambda rows: next(
            (t for t in rows_of(rows, "task") if t.get("policyRef") == f"SCHED#{sid}" and t["taskId"] != first_id), None
        ),
        FIRE_S,
    )

    await api.post(f"{SCHEDULES}/{sid}/pause")
    rows = await ctx.tasks_rows(ctx.ws)
    count = len([t for t in rows_of(rows, "task") if t.get("policyRef") == f"SCHED#{sid}"])
    cur = (await api.get(f"{SCHEDULES}/{sid}")).raise_for_status().json()
    await api.patch(
        f"{SCHEDULES}/{sid}", json={"schedule": {"cron": [_cron_in(2, tz)], "tz": tz}, "version": cur["version"]}
    )
    await ctx.sleep(FIRE_S)
    after = len([t for t in rows_of(await ctx.tasks_rows(ctx.ws), "task") if t.get("policyRef") == f"SCHED#{sid}"])
    runs = (await api.get(f"{SCHEDULES}/{sid}/runs")).raise_for_status().json()["runs"]
    checks = {
        "draft_row_with_cron": draft["status"] == "draft" and draft["cron"] == ["0 9 * * 1"],
        "confirm_card_posted": confirm is not None,
        "router_text_has_no_clock_time": ":" not in router_text and " am" not in router_text.lower(),
        "yes_leaves_draft": still_draft,
        "next_run_is_monday_nine": monday_nine,
        "run_now_made_a_task_with_policy_ref": run1 is not None,
        "second_run_now_is_409": second_now.status_code == 409,
        "one_off_fired_a_second_task": run2 is not None and (run2.get("previousRunTaskId") == first_id),
        "paused_schedule_did_not_fire": after == count,
        "history_lists_both_runs": {first_id, (run2 or {}).get("taskId")}
        <= {r.get("id") or r.get("taskId") for r in runs},
    }
    return Result(
        all(checks.values()), {"scheduleId": sid, "tz": tz, "taskIds": [first_id, (run2 or {}).get("taskId")], **checks}
    )
