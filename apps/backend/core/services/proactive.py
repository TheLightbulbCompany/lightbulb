"""The proactive layer of a super-agent workspace (product shape, 5 Nov 2026): Ideas start Router requests, the
first run files 3 ideas, and two platform automations (a daily look around that files ideas, scheduled feed posts)
run for paying workspaces only, under one daily spend cap.

No new infrastructure: requests are scripted `start_task` triggers (as automation fires are, C31); the automations
are ordinary `SCHED#` rows owned by the workspace (Temporal Schedules, per-run budget, 3-day idle skip, pause).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("queued", "working", "waiting", "idle")
LOOK_AROUND_ID = "builtin:look_around"
FEED_ID = "builtin:feed"
FIRST_RUN_KEY = "first-run"
RUN_BUDGET_USD = 0.25
FIRST_RUN_BUDGET_USD = 0.50
#: Every proactive run of one workspace in one UTC day (first run, look around, feed posts) together.
DAILY_CAP_USD = 1.00
NOT_PAYING = "not_paying"
_RECENT_RUNS = 8  # per automation: more than a day's runs (look around 1, feed 3)

FIRST_RUN_TEXT = (
    "First look at this company. Find out what it does and what it needs now: what the team told Lightbulb "
    "when they joined and in #general, the company's website if they gave one, and the brief below. Then file "
    "exactly 3 ideas with the suggest_idea tool: concrete jobs for this company the team could start today, each "
    "with a real reason from what you found. Do not post in chat."
)
LOOK_AROUND_TEXT = (
    "Daily look around. Read the brief and the recent activity below, then look at what is going on in this "
    "company: its rooms, recent requests, connected tools and, when it helps, the web. File at most 3 ideas with "
    "the suggest_idea tool: concrete jobs for this company the team could start today, each with a real reason "
    "from what you saw. Never repeat an idea listed below or one the team said Not now to. If nothing is worth "
    "suggesting, file nothing. Do not post in chat."
)
FEED_TEXT = (
    "Scheduled feed post. Read the brief and the recent activity below, then look at what happened since the "
    "last scheduled feed post: rooms, requests and connected tools. Read the feed's recent posts first and never "
    "repeat one (the lightbulb skill's references/feed.md). For each thing worth the team's time, post ONE feed "
    "post about it, at most 3 this run:\n\n"
    "/app/skills/lightbulb/scripts/lightbulb-post.sh --feed --why \"<why it matters>\" <<'EOF'\n<post>\nEOF\n\n"
    "If nothing matters, post nothing."
)


def automation_rows(ws: str, tz: str) -> list[dict]:
    """The two platform automations, owned by the workspace (any org admin can pause them)."""
    common = {"ownerUserId": ws, "tz": tz, "budgetPerRunUsd": RUN_BUDGET_USD, "deliverTo": "none"}
    return [
        {**common, "scheduleId": LOOK_AROUND_ID, "name": "Daily look around", "cron": ["0 7 * * *"],
         "policyText": LOOK_AROUND_TEXT},
        {**common, "scheduleId": FEED_ID, "name": "Feed posts", "cron": ["0 8 * * *", "0 13 * * *", "0 18 * * *"],
         "policyText": FEED_TEXT},
    ]  # fmt: skip


def is_proactive(schedule_id: str) -> bool:
    return schedule_id in (LOOK_AROUND_ID, FEED_ID)


def first_run_task_id() -> str:
    return ts.router_task_id(ts.trigger_id("idea", FIRST_RUN_KEY), 0, 0)


async def paying(ws: str) -> bool:
    """Paid plan only (the paid-only predicate for autonomous features); a free trial is not paying."""
    from core.repositories import billing_repo
    from core.services.provision_gate import has_always_on_entitlement

    return has_always_on_entitlement(await billing_repo.get_by_owner_id(ws))


async def _timezone(ws: str) -> str:
    """The workspace creator's own zone, else UTC (as the old feed routines did)."""
    from core.repositories import user_repo
    from core.services import clerk_admin

    try:
        creator = (await clerk_admin.get_organization(ws) or {}).get("created_by")
        tz = (await user_repo.get(creator) or {}).get("timezone") if creator else None
    except Exception:  # noqa: BLE001 -- a zone is a nicety, never a reason to skip
        logger.warning("proactive: no creator zone for %s", ws, exc_info=True)
        tz = None
    return tz if user_repo.valid_timezone(tz) else "UTC"


async def ensure(ws: str) -> None:
    """Idempotent, both ways: the first run once per workspace; the two automations on while it pays, paused
    (`paused_system: not_paying`) when it stops. An owner's own pause is theirs to keep. Switch off: nothing."""
    from core.repositories import automations_repo as repo
    from core.services import automation_schedule, super_agent_flags

    from core.repositories import tasks_repo

    if not await super_agent_flags.flags_for(ws):
        return
    if await tasks_repo.get_trigger(ws, ts.trigger_id("idea", FIRST_RUN_KEY)) is None:  # once, ever
        await start_request(
            ws,
            key=FIRST_RUN_KEY,
            title="First ideas for your company",
            brief=FIRST_RUN_TEXT + "\n\n" + await context(ws),
            budget_usd=FIRST_RUN_BUDGET_USD,
        )
    is_paying = await paying(ws)
    tz: str | None = None
    for want in automation_rows(ws, "UTC"):
        row = await repo.get_schedule(ws, want["scheduleId"])
        if row is None:
            if not is_paying:
                continue
            tz = tz or await _timezone(ws)
            row, _ = await repo.create_schedule(ws, {**want, "tz": tz})
        status, reason = row["status"], row.get("pausedReason")
        if is_paying and (status == "draft" or (status == "paused_system" and reason == NOT_PAYING)):
            row = await repo.set_status(ws, row["scheduleId"], "active", reason=None, allowed_from=frozenset({status}))
        elif not is_paying and status in ("draft", "active"):
            row = await repo.set_status(
                ws, row["scheduleId"], "paused_system", reason=NOT_PAYING, allowed_from=frozenset({status})
            )
        else:
            continue
        await automation_schedule.upsert(row)


def _today() -> str:
    return utc_now_iso()[:10]


async def spent_today(ws: str) -> float:
    """Recorded cost of today's (UTC) proactive runs: the first run and the two automations' recent runs.
    ponytail: a few task-tree reads per fire (≤ 4 fires a day); a per-day counter row if runs get frequent."""
    from core.repositories import automations_repo as repo
    from core.repositories import tasks_repo

    ids = {first_run_task_id()}
    for sid in (LOOK_AROUND_ID, FEED_ID):
        row = await repo.get_schedule(ws, sid) or {}
        ids.update(t for t in [row.get("activeRunTaskId"), *(row.get("runTaskIds") or [])[:_RECENT_RUNS]] if t)
    today, total = _today(), 0.0
    for task_id in ids:
        tree = await tasks_repo.get_task_tree(ws, task_id)
        if tree["task"] and str(tree["task"].get("createdAt") or "").startswith(today):
            total += sum(float((a.get("usage") or {}).get("costUsd") or 0) for a in tree["attempts"])
    return total


async def fire_check(ws: str, sid: str) -> tuple[str | None, float]:
    """Before a proactive fire: `(skip reason, None)` or `(None, what is left of today's cap)`. Logged."""
    if not await paying(ws):
        return NOT_PAYING, 0.0
    spent = await spent_today(ws)
    logger.info("proactive spend ws=%s automation=%s today=%.4f cap=%.2f", ws, sid, spent, DAILY_CAP_USD)
    put_metric("proactive.spend_today_usd", value=spent, unit="None")
    if spent >= DAILY_CAP_USD:
        put_metric("proactive.daily_cap_hit")
        return "daily_cap", 0.0
    return None, DAILY_CAP_USD - spent


async def _brief(ws: str) -> str:
    """The feed room's topic: the team's brief (the default until the feed exists)."""
    from core.repositories import messaging_repo
    from core.services.messaging import FEED_DEFAULT_TOPIC

    meta = await messaging_repo.get_room(messaging_repo.feed_room_id(ws)) or {}
    return meta.get("topic") or FEED_DEFAULT_TOPIC


async def context(ws: str) -> str:
    """The brief plus recent activity: open and finished requests, ideas listed, ideas recently turned down."""
    from core.repositories import messaging_repo, tasks_repo
    from core.services.ideas import DISMISS_MEMORY

    lines = [f"The brief (what the team wants to hear about): {await _brief(ws)}"]
    open_titles = [t.get("title") for t in await open_requests(ws)][:20]
    done, _ = await tasks_repo.list_tasks_by_status(ws, "resolved", limit=10)
    dismissed = await messaging_repo.dismissed_idea_times(ws)
    cutoff = (datetime.now(timezone.utc) - DISMISS_MEMORY).isoformat()
    ideas = await messaging_repo.suggested_ideas(ws)
    listed = [i["title"] for i in ideas if i["ideaId"] not in dismissed]
    turned_down = [i["title"] for i in ideas if dismissed.get(i["ideaId"], "") >= cutoff]
    for heading, titles in (
        ("Requests in progress", open_titles),
        ("Recently finished requests", [t.get("title") for t in done]),
        ("Ideas already listed", listed),
        ("Ideas the team said Not now to (do not suggest again)", turned_down),
    ):
        titles = [t for t in titles if t]
        if titles:
            lines.append(f"{heading}:\n" + "\n".join(f"- {t}" for t in titles))
    return "\n\n".join(lines)


async def start_request(
    ws: str,
    *,
    key: str,
    title: str,
    brief: str,
    actor_user_id: str | None = None,
    room_id: str | None = None,
    specialist: str | None = None,
    budget_usd: float | None = None,
) -> str:
    """One Router request through admission, applied without a model turn (a scripted `start_task`). Idempotent
    on `key` (the trigger id, hence the task id). With a person it is theirs (their private lane, answered in
    `room_id`); without one it is the workspace's. Returns the task id."""
    from core.services import admission

    trigger_id = ts.trigger_id("idea", key)
    task_id = ts.router_task_id(trigger_id, 0, 0)
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=title[:200],
        brief=brief,
        specialist=specialist,
        ack=f"On it: {title}",
        visibility="person" if actor_user_id else "workspace",
        budget_usd=budget_usd,
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=ws,
        trigger_id=trigger_id,
        source="idea",
        admission="respond",
        lane=ts.router_lane_key(actor_user_id) if actor_user_id else "shared",
        actor_user_id=actor_user_id,
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={"taskId": task_id, "start": start.model_dump(by_alias=True)},
    )
    await admission.enqueue_trigger(ws, trigger, room_id=room_id)
    return task_id


async def open_requests(ws: str) -> list[dict]:
    """The workspace's requests still going (newest first per status)."""
    from core.repositories import tasks_repo

    out: list[dict] = []
    for status in OPEN_STATUSES:
        rows, _ = await tasks_repo.list_tasks_by_status(ws, status, limit=50)
        out.extend(rows)
    return out


async def request_open(ws: str, task_id: str) -> bool:
    from core.repositories import tasks_repo

    task = await tasks_repo.get_task(ws, task_id)
    return bool(task) and task.get("status") in OPEN_STATUSES
