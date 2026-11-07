"""The proactive layer of a super-agent workspace (product shape, 5 Nov 2026): Ideas start Router requests, the
first run files 3 ideas, and two platform automations run under one daily spend cap: a daily look around that files
ideas (paying workspaces only) and scheduled feed posts (every workspace, founder 5 Nov 2026).

No new infrastructure: requests are scripted `start_task` triggers (as automation fires are, C31); the automations
are ordinary `SCHED#` rows owned by the workspace (Temporal Schedules, per-run budget, 3-day idle skip, pause).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, plain, run_in_thread, utc_now_iso
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

FIRST_RUN_TEXT = (
    "First look at this company. Find out what it does and what it needs now: what the team told Lightbulb "
    "when they joined and in #general, the company's website if they gave one, and the brief below. Then file "
    "exactly 3 ideas with the suggest_idea tool: concrete jobs for this company the team could start today, each "
    "with a real reason from what you found."
)
LOOK_AROUND_TEXT = (
    "Daily look around. Read the brief and the recent activity below, then look at what is going on in this "
    "company: its projects, recent tasks, connected tools and, when it helps, the web. File at most 3 ideas with "
    "the suggest_idea tool: concrete jobs for this company the team could start today, each with a real reason "
    "from what you saw. Never repeat an idea listed below or one the team said Not now to. If nothing is worth "
    "suggesting, file nothing. Do not post in chat."
)
FEED_TEXT = (
    "Scheduled feed post. Read the brief and the recent activity below, then look at what happened since the "
    "last scheduled feed post: projects, tasks and connected tools. Read the feed's recent posts first and never "
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


def paying_only(schedule_id: str) -> bool:
    """The look around is for paying workspaces; feed posts run for everyone (founder, 5 Nov 2026)."""
    return schedule_id == LOOK_AROUND_ID


def first_run_task_id() -> str:
    return ts.router_task_id(ts.trigger_id("idea", FIRST_RUN_KEY), 0, 0)


async def paying(ws: str) -> bool:
    """Paid plan only (the paid-only predicate for autonomous features); a free trial is not paying."""
    from core.repositories import billing_repo
    from core.services.provision_gate import has_always_on_entitlement

    return has_always_on_entitlement(await billing_repo.get_by_owner_id(ws))


async def live(ws: str) -> bool:
    """May use the platform: paid, or a free trial still running (not canceled, expired or deleted)."""
    from core.repositories import billing_repo
    from core.services.provision_gate import should_arm_nightly_ceo

    return should_arm_nightly_ceo(await billing_repo.get_by_owner_id(ws))


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


async def ensure(ws: str, *, backfill: bool = False) -> None:
    """Idempotent, both ways: the first run once per workspace; feed posts on while the workspace may use the
    platform (paid, or a live free trial), the look around while it pays; otherwise `paused_system: not_paying`.
    An owner's own pause is theirs to keep. Switch off: nothing. `backfill` (the daily reconcile): no first run
    for a dormant workspace; provisioning and billing changes (`reconcile_runtime`, the provision runner) skip that check."""
    from core.repositories import automations_repo as repo
    from core.services import automation_schedule, super_agent_flags

    from core.repositories import tasks_repo

    if not await super_agent_flags.flags_for(ws):
        return
    # Once, ever; never for a dormant workspace (a deploy's first reconcile reaches every switched-on one).
    first_run_due = await tasks_repo.get_trigger(ws, ts.trigger_id("idea", FIRST_RUN_KEY)) is None
    if first_run_due and not (backfill and await _dormant(ws)):
        budget = await reserve(ws, FIRST_RUN_KEY, FIRST_RUN_BUDGET_USD)
        if budget:
            await start_request(
                ws,
                key=FIRST_RUN_KEY,
                title="First ideas for your company",
                brief=FIRST_RUN_TEXT + "\n\n" + await context(ws),
                actor_user_id=ws,
                budget_usd=budget,
            )
    is_paying, is_live = await paying(ws), await live(ws)
    tz: str | None = None
    for want in automation_rows(ws, "UTC"):
        on = is_paying if paying_only(want["scheduleId"]) else is_live
        row = await repo.get_schedule(ws, want["scheduleId"])
        if row is None:
            if not on:
                continue
            tz = tz or await _timezone(ws)
            row, _ = await repo.create_schedule(ws, {**want, "tz": tz})
        status, reason = row["status"], row.get("pausedReason")
        if on and (status == "draft" or (status == "paused_system" and reason == NOT_PAYING)):
            row = await repo.set_status(ws, row["scheduleId"], "active", reason=None, allowed_from=frozenset({status}))
        elif not on and status in ("draft", "active"):
            row = await repo.set_status(
                ws, row["scheduleId"], "paused_system", reason=NOT_PAYING, allowed_from=frozenset({status})
            )
        else:
            continue
        await automation_schedule.upsert(row)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _dormant(ws: str) -> bool:
    """No human activity for the automations' idle window, judged strictly (no container, or never used since
    created over the window ago, is dormant)."""
    from core.temporal.automations import _idle

    return await _idle(ws, _now_ms(), strict=True)


def _today() -> str:
    return utc_now_iso()[:10]


def _budget_key(ws: str) -> dict:
    return {"pk": ts.ws_pk(ws), "sk": f"PROACTIVE#{_today()}"}


async def _budget_row(ws: str) -> dict:
    from core.repositories import tasks_repo

    got = await run_in_thread(tasks_repo._table().get_item, Key=_budget_key(ws), ConsistentRead=True)
    return plain(got.get("Item") or {})


async def _swap(ws: str, row: dict, reserved: float, runs: dict) -> bool:
    """Write the day's row if nobody changed it since `row` was read."""
    from botocore.exceptions import ClientError

    from core.repositories import tasks_repo

    version = int(row.get("version") or 0)
    item = {**_budget_key(ws), "type": "proactive_budget", "reservedUsd": round(reserved, 4), "runs": runs,
            "version": version + 1}  # fmt: skip
    try:
        await run_in_thread(
            tasks_repo._table().put_item,
            Item=ddb_safe(item),
            ConditionExpression="attribute_not_exists(pk) OR version = :v",
            ExpressionAttributeValues={":v": version},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


async def reserved_today(ws: str) -> float:
    return float((await _budget_row(ws)).get("reservedUsd") or 0)


async def reserve(ws: str, run_key: str, amount: float) -> float:
    """Hold up to `amount` of today's (UTC) proactive cap for one run, before it starts: concurrent fires and Run
    now can't both take the same headroom. Returns what was held (0 = the cap is used up); idempotent per
    `run_key`, so a retried fire holds nothing more. The held amount is the run's budget.
    ponytail: one row per workspace per day, never deleted (a few bytes); a TTL if it ever matters."""
    for _ in range(8):
        row = await _budget_row(ws)
        runs = dict(row.get("runs") or {})
        if run_key in runs:
            return float(runs[run_key])
        reserved = float(row.get("reservedUsd") or 0)
        take = round(min(amount, DAILY_CAP_USD - reserved), 4)
        if take <= 0:
            logger.info(
                "proactive cap reached ws=%s run=%s reserved=%.2f cap=%.2f", ws, run_key, reserved, DAILY_CAP_USD
            )
            put_metric("proactive.daily_cap_hit")
            return 0.0
        if await _swap(ws, row, reserved + take, {**runs, run_key: take}):
            logger.info("proactive reserve ws=%s run=%s usd=%.2f today=%.2f", ws, run_key, take, reserved + take)
            put_metric("proactive.reserved_today_usd", value=reserved + take, unit="None")
            return take
    raise RuntimeError("proactive budget row contended")  # the Activity's retry policy tries again


async def release(ws: str, run_key: str) -> None:
    """Give a run's hold back (it never started: an overlap)."""
    for _ in range(8):
        row = await _budget_row(ws)
        runs = dict(row.get("runs") or {})
        if run_key not in runs:
            return
        take = float(runs.pop(run_key))
        if await _swap(ws, row, max(0.0, float(row.get("reservedUsd") or 0) - take), runs):
            return
    raise RuntimeError("proactive budget row contended")


async def fire_budget(ws: str, sid: str, run_key: str, budget: float | None) -> tuple[str | None, float]:
    """Before a proactive fire: `(skip reason, 0)` or `(None, the run's budget held against today's cap)`."""
    if not await (paying(ws) if paying_only(sid) else live(ws)):
        return NOT_PAYING, 0.0
    held = await reserve(ws, run_key, budget or RUN_BUDGET_USD)
    return (None, held) if held else ("daily_cap", 0.0)


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
    # Only the workspace's own requests: this text becomes a workspace-visible brief (and maybe a feed post), so a
    # person's private request or a room's never shows here.
    shared = lambda rows: [t for t in rows if (t.get("audience") or {}).get("kind") == "workspace"]  # noqa: E731
    open_titles = [t.get("title") for t in shared(await open_requests(ws))][:20]
    done, _ = await tasks_repo.list_tasks_by_status(ws, "resolved", limit=30)
    done = shared(done)[:10]
    dismissed = await messaging_repo.dismissed_idea_times(ws)
    cutoff = (datetime.now(timezone.utc) - DISMISS_MEMORY).isoformat()
    ideas = await messaging_repo.suggested_ideas(ws)
    listed = [i["title"] for i in ideas if i["ideaId"] not in dismissed]
    turned_down = [i["title"] for i in ideas if dismissed.get(i["ideaId"], "") >= cutoff]
    for heading, titles in (
        ("Tasks in progress", open_titles),
        ("Recently finished tasks", [t.get("title") for t in done]),
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
    row_now: bool = False,
) -> str:
    """One Router request through admission, applied without a model turn (a scripted `start_task`). Idempotent
    on `key` (the trigger id, hence the task id). With a person it is theirs (their private lane, answered in
    `room_id`); with the workspace as the actor it is the workspace's (shared lane, metered to the workspace,
    its admins answer its questions). `row_now`: the request's row is written before this returns, for a client
    that opens it at once (an idea's Do it). Returns the task id."""
    from core.services import admission

    person = bool(actor_user_id) and actor_user_id != ws

    trigger_id = ts.trigger_id("idea", key)
    task_id = ts.router_task_id(trigger_id, 0, 0)
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=title[:200],
        brief=brief,
        specialist=specialist,
        ack=f"On it: {title}",
        visibility="person" if person else "workspace",
        budget_usd=budget_usd,
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=ws,
        trigger_id=trigger_id,
        source="idea",
        admission="respond",
        lane=ts.router_lane_key(actor_user_id) if person else "shared",
        actor_user_id=actor_user_id,
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={"taskId": task_id, "start": start.model_dump(by_alias=True)},
    )
    await admission.enqueue_trigger(ws, trigger, room_id=room_id)
    if row_now:
        await _row_now(ws, trigger, task_id, start, room_id)
    return task_id


async def _row_now(ws: str, trigger: ts.Trigger, task_id: str, start: ts.StartTask, room_id: str | None) -> None:
    """The row the Router's scripted start writes (same id, same fields: it then finds this one), written now. Dev
    walk 7 (W7-1): Do it opened the request ~25 s before the Router's queue reached it ("This request isn't
    available to you"). A start the chat gate refuses gets no row: the Router posts why and starts nothing."""
    from core.services import chat_gate
    from core.temporal import activities

    gate = await chat_gate.gate_chat(user_id=trigger.actor_user_id or "", owner_id=ws, session_key=f"task:{task_id}")
    if gate.get("blocked"):
        return
    item = trigger.model_dump(by_alias=True)
    item["ref"] = {**item.get("ref", {}), **({"deliverRoomId": room_id} if room_id else {})}
    await activities._ensure_task(
        ws,
        task_id,
        item,
        trigger.lane,
        "queued",
        title=start.title,
        description=start.brief,
        specialist=start.specialist,
        visibility=start.visibility,
        budgetUsd=start.budget_usd,
    )


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
