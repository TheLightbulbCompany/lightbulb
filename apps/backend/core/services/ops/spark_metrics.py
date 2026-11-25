"""Spark product metrics for the admin console: HogQL over PostHog, cached 5 min.

Events come from the desktop app via POST /os/event (core/observability/spark_events.py)
and from the server (account_created, subscription_*, ...). Every query excludes
internal accounts (see ``_internal_filter``) and other environments.

Definitions (each query below restates its own):
1. Weekly Active Doers (north star) - distinct users with >=3 ``task_completed`` in the week.
2. New activated users - installs whose first ``task_completed`` is within 24h of
   ``app_first_launch``, split by ``platform`` (missing -> "unknown").
3. Activation rate - activated installs / installs, per weekly install cohort.
4. Week-4 retention - installs with a ``task_completed`` in days 22-28 after launch / installs,
   per weekly install cohort (only once the cohort is old enough to have a full day-28).
5. Task success rate - task_completed / (task_completed + task_failed) events per week, plus the
   top ``task_failed.reason`` and ``problem_raised.kind`` of the last 7 days.
Funnel (30 days, distinct users per stage, not strictly sequential) and a Reliability panel
(installs reporting a ``health`` event with brain != ok in the last 24h).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone

from core.config import settings
from core.services import posthog_admin
from core.services.ops import envelope as env
from core.services.ops.growth import _hogql_filter

WEEKS = 12
RETENTION_DAYS = (22, 28)  # inclusive day range after first launch
CACHE_TTL_S = 300
FUNNEL_STAGES = (
    "spark_download_clicked",
    "app_first_launch",
    "signin_completed",
    "onboarding_completed",
    "task_completed",
    "subscription_started",
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


def _internal_filter() -> str:
    """Exclude team accounts: Clerk ids in SPARK_INTERNAL_USER_IDS / PLATFORM_ADMIN_USER_IDS,
    and any PostHog person with the ``is_internal`` property set to true."""
    ids = sorted(i for i in settings.spark_internal_user_ids if _SAFE_ID.match(i))
    clause = "coalesce(toString(person.properties.is_internal), '') != 'true'"
    if ids:
        clause += f" and distinct_id not in ({', '.join(repr(i) for i in ids)})"
    return clause


def _where() -> str:
    return f"{_hogql_filter(settings.ENVIRONMENT or '')} and {_internal_filter()}"


def build_queries() -> dict[str, str]:
    w = _where()
    d0, d1 = RETENTION_DAYS
    # Per-install launch time + platform (first launch wins); shared by metrics 2-4.
    installs = f"""(
      select distinct_id, min(timestamp) as launch,
             argMin(coalesce(nullIf(toString(properties.platform), ''), 'unknown'), timestamp) as platform
      from events
      where event = 'app_first_launch' and {w} and timestamp >= now() - interval {WEEKS} week
      group by distinct_id)"""
    return {
        # 1. Weekly Active Doers: users with >=3 task_completed in the calendar week (Mon-start).
        "doers_weekly": f"""
            select wk, count() as doers from (
              select toStartOfWeek(timestamp, 1) as wk, distinct_id
              from events where event = 'task_completed' and {w} and timestamp >= now() - interval {WEEKS} week
              group by wk, distinct_id having count() >= 3)
            group by wk order by wk limit 100""",
        # 1b. Headline: the same definition over a rolling 7 days.
        "doers_7d": f"""
            select count() from (
              select distinct_id from events
              where event = 'task_completed' and {w} and timestamp >= now() - interval 7 day
              group by distinct_id having count() >= 3)""",
        # 2+3. Per weekly install cohort and platform: installs, and installs whose first
        # task_completed landed within 24h after launch (activated). Activation rate = activated/installs.
        "activation": f"""
            select toStartOfWeek(l.launch, 1) as cohort, l.platform as platform,
                   count() as installs,
                   countIf(t.first_task >= l.launch and t.first_task <= l.launch + interval 24 hour) as activated
            from {installs} as l
            left join (select distinct_id, min(timestamp) as first_task from events
                       where event = 'task_completed' and {w} group by distinct_id) as t
              on l.distinct_id = t.distinct_id
            group by cohort, platform order by cohort limit 500""",
        # 4. Week-4 retention numerator: installs per cohort with >=1 task_completed in days {d0}-{d1}.
        "retention": f"""
            select cohort, count() as retained from (
              select l.distinct_id as distinct_id, toStartOfWeek(l.launch, 1) as cohort
              from {installs} as l
              join (select distinct_id, timestamp from events
                    where event = 'task_completed' and {w}) as e on l.distinct_id = e.distinct_id
              where e.timestamp >= l.launch + interval {d0} day and e.timestamp < l.launch + interval {d1 + 1} day
              group by distinct_id, cohort)
            group by cohort order by cohort limit 100""",
        # 5a. Task success: weekly event counts of task_completed vs task_failed.
        "tasks_weekly": f"""
            select toStartOfWeek(timestamp, 1) as wk, event, count() as n
            from events where event in ('task_completed', 'task_failed') and {w}
              and timestamp >= now() - interval {WEEKS} week
            group by wk, event order by wk limit 500""",
        # 5b. Top task_failed.reason, last 7 days.
        "failed_reasons": f"""
            select coalesce(nullIf(toString(properties.reason), ''), 'unknown') as k, count() as n
            from events where event = 'task_failed' and {w} and timestamp >= now() - interval 7 day
            group by k order by n desc limit 10""",
        # 5c. Top problem_raised.kind, last 7 days.
        "problem_kinds": f"""
            select coalesce(nullIf(toString(properties.kind), ''), 'unknown') as k, count() as n
            from events where event = 'problem_raised' and {w} and timestamp >= now() - interval 7 day
            group by k order by n desc limit 10""",
        # Funnel, last 30 days: distinct users per stage. subscription_started counts only Spark's.
        "funnel": f"""
            select event, uniq(distinct_id) as users
            from events
            where event in ({", ".join(repr(s) for s in FUNNEL_STAGES)}) and {w}
              and (event != 'subscription_started' or toString(properties.product) = 'spark')
              and timestamp >= now() - interval 30 day
            group by event limit 20""",
        # Reliability: installs whose `health` event reported brain != ok in the last 24h.
        "unhealthy": f"""
            select uniq(distinct_id) from events
            where event = 'health' and {w} and timestamp >= now() - interval 24 hour
              and coalesce(toString(properties.brain), '') not in ('', 'ok')""",
    }


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def derive(rows: dict[str, list], now: datetime) -> dict:
    """Raw HogQL result rows (by query name) -> the JSON payload. Pure; no I/O."""
    today = now.date()
    weeks = [(_monday(today) - timedelta(weeks=i)).isoformat() for i in range(WEEKS - 1, -1, -1)]
    day = lambda v: str(v)[:10]  # noqa: E731  PostHog returns dates as ISO strings

    doers = {day(r[0]): int(r[1]) for r in rows["doers_weekly"]}
    tasks: dict[str, dict[str, int]] = {}
    for wk, event, n in rows["tasks_weekly"]:
        tasks.setdefault(day(wk), {})[event] = int(n)

    cohort_installs: dict[str, int] = {}
    cohort_activated: dict[str, int] = {}
    platforms: dict[str, dict[str, dict[str, int]]] = {}  # week -> platform -> {installs, activated}
    for cohort, platform, installs, activated in rows["activation"]:
        c = day(cohort)
        cohort_installs[c] = cohort_installs.get(c, 0) + int(installs)
        cohort_activated[c] = cohort_activated.get(c, 0) + int(activated)
        platforms.setdefault(c, {})[platform] = {"installs": int(installs), "activated": int(activated)}
    retained = {day(c): int(n) for c, n in rows["retention"]}
    platform_names = sorted({p for per in platforms.values() for p in per})

    cohorts = []
    for wk in weeks:
        installs, activated = cohort_installs.get(wk, 0), cohort_activated.get(wk, 0)
        # A cohort is measurable once its last install has reached day 28 + the week's width.
        mature = date.fromisoformat(wk) + timedelta(days=7 + RETENTION_DAYS[1]) <= today
        cohorts.append(
            {
                "week": wk,
                "installs": installs,
                "activated": activated,
                "activation_rate": _rate(activated, installs),
                "w4_retained": retained.get(wk, 0) if mature else None,
                "w4_retention": _rate(retained.get(wk, 0), installs) if mature else None,
                "activated_by_platform": {
                    p: platforms.get(wk, {}).get(p, {}).get("activated", 0) for p in platform_names
                },
            }
        )

    return {
        "generated_at": now.isoformat(),
        "weeks": weeks,
        "wad": {
            "rolling_7d": int(rows["doers_7d"][0][0]) if rows["doers_7d"] else 0,
            "weekly": [{"week": wk, "doers": doers.get(wk, 0)} for wk in weeks],
        },
        "platforms": platform_names,
        "cohorts": cohorts,
        "task_success": {
            "weekly": [
                {
                    "week": wk,
                    "completed": tasks.get(wk, {}).get("task_completed", 0),
                    "failed": tasks.get(wk, {}).get("task_failed", 0),
                    "rate": _rate(
                        tasks.get(wk, {}).get("task_completed", 0),
                        tasks.get(wk, {}).get("task_completed", 0) + tasks.get(wk, {}).get("task_failed", 0),
                    ),
                }
                for wk in weeks
            ],
            "top_failed_reasons": [{"key": str(k), "count": int(n)} for k, n in rows["failed_reasons"]],
            "top_problem_kinds": [{"key": str(k), "count": int(n)} for k, n in rows["problem_kinds"]],
        },
        "funnel": [
            {"stage": s, "users": dict((e, int(u)) for e, u in rows["funnel"]).get(s, 0)} for s in FUNNEL_STAGES
        ],
        "reliability": {"unhealthy_installs_24h": int(rows["unhealthy"][0][0]) if rows["unhealthy"] else 0},
    }


_cache: tuple[float, dict, str, list[dict]] | None = None


async def compute() -> tuple[dict | None, str, list[dict]]:
    """Run every query (one failure degrades to ``partial``/``unavailable``, never fake zeros)."""
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    queries = build_queries()
    results = await asyncio.gather(*(posthog_admin.hogql(q) for q in queries.values()))
    by_name = dict(zip(queries, results))

    if all(r["stubbed"] for r in results):
        return None, env.UNAVAILABLE, [{"domain": "posthog", "reason": "not configured"}]
    errors = [{"domain": f"posthog:{n}", "reason": r["error"]} for n, r in by_name.items() if r["error"]]
    if len(errors) == len(results):
        return None, env.UNAVAILABLE, errors

    # A failed query's section renders empty (rows = []), flagged via errors; the page shows the banner.
    data = derive({n: r["results"] for n, r in by_name.items()}, datetime.now(timezone.utc))
    data["failed_queries"] = [e["domain"].split(":", 1)[1] for e in errors]
    state = env.PARTIAL if errors else env.OK
    if not errors:
        _cache = (time.monotonic() + CACHE_TTL_S, data, state, errors)
    return data, state, errors
