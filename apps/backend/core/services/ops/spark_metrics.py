"""Spark product metrics for the admin console: HogQL over PostHog, cached 5 min.

Events come from the desktop app via POST /os/event (core/observability/spark_events.py)
and from the server (account_created, subscription_*, ...). Every query excludes
internal accounts (see ``_internal_filter``) and other environments.

Identity: ``app_first_launch`` is sent pre-sign-in under the install UUID, later events carry the
Clerk id, and POST /os/event aliases install -> person at sign-in. Everything per-user is therefore
keyed on HogQL ``person_id`` (alias-resolved), not ``distinct_id``. Installs = distinct person_id with
``app_first_launch``; the ``install_id`` property was rejected as the key because it would count one
person on two machines twice and never joins to the later events. (An install never signed in stays
its own person, which is the right "install" count.)

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
    """Exclude team accounts: every person (alias-resolved, so their pre-sign-in install events too)
    that owns a Clerk id in SPARK_INTERNAL_USER_IDS / PLATFORM_ADMIN_USER_IDS."""
    ids = sorted(i for i in settings.spark_internal_user_ids if _SAFE_ID.match(i))
    if not ids:
        return "1 = 1"
    id_list = ", ".join(repr(i) for i in ids)
    return f"person_id not in (select person_id from person_distinct_ids where distinct_id in ({id_list}))"


def _where() -> str:
    return f"{_hogql_filter(settings.ENVIRONMENT or '')} and {_internal_filter()}"


def build_queries() -> dict[str, str]:
    w = _where()
    d0, d1 = RETENTION_DAYS

    # Per-install launch time + platform (first launch wins); shared by metrics 2-4.
    def installs(settled: bool = False) -> str:
        # `settled`: only installs a full 24h old, so a fresh install can't read as "not activated" yet.
        having = " having min(timestamp) <= now() - interval 24 hour" if settled else ""
        return f"""(
      select person_id, min(timestamp) as launch,
             argMin(coalesce(nullIf(toString(properties.platform), ''), 'unknown'), timestamp) as platform
      from events
      where event = 'app_first_launch' and {w} and timestamp >= now() - interval {WEEKS} week
      group by person_id{having})"""

    task_window = f"timestamp >= now() - interval {WEEKS + 1} week"  # bound the task_completed scans
    return {
        # 1. Weekly Active Doers: users with >=3 task_completed in the calendar week (Mon-start).
        "doers_weekly": f"""
            select wk, count() as doers from (
              select toStartOfWeek(timestamp, 1) as wk, person_id
              from events where event = 'task_completed' and {w} and timestamp >= now() - interval {WEEKS} week
              group by wk, person_id having count() >= 3)
            group by wk order by wk limit 100""",
        # 1b. Headline: the same definition over a rolling 7 days.
        "doers_7d": f"""
            select count() from (
              select person_id from events
              where event = 'task_completed' and {w} and timestamp >= now() - interval 7 day
              group by person_id having count() >= 3)""",
        # 2+3. Per weekly install cohort and platform: installs, and installs whose first
        # task_completed landed within 24h after launch (activated). Activation rate = activated/installs.
        "activation": f"""
            select toStartOfWeek(l.launch, 1) as cohort, l.platform as platform,
                   count() as installs,
                   countIf(t.first_task >= l.launch and t.first_task <= l.launch + interval 24 hour) as activated
            from {installs(settled=True)} as l
            left join (select person_id, min(timestamp) as first_task from events
                       where event = 'task_completed' and {w} and {task_window} group by person_id) as t
              on l.person_id = t.person_id
            group by cohort, platform order by cohort limit 500""",
        # 4. Week-4 retention numerator: installs per cohort with >=1 task_completed in days {d0}-{d1}.
        "retention": f"""
            select cohort, count() as retained from (
              select l.person_id as person_id, toStartOfWeek(l.launch, 1) as cohort
              from {installs()} as l
              join (select person_id, timestamp from events
                    where event = 'task_completed' and {w} and {task_window}) as e on l.person_id = e.person_id
              where e.timestamp >= l.launch + interval {d0} day and e.timestamp < l.launch + interval {d1 + 1} day
              group by person_id, cohort)
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
            select event, uniq(person_id) as users
            from events
            where event in ({", ".join(repr(s) for s in FUNNEL_STAGES)}) and {w}
              and (event != 'subscription_started' or toString(properties.product) = 'spark')
              and timestamp >= now() - interval 30 day
            group by event limit 20""",
        # Has the event ever been seen? Distinguishes "no data yet" from a green 0.
        "seen_events": f"""
            select event, count() from events
            where event in ('health', 'problem_raised') and {w} and timestamp >= now() - interval 90 day
            group by event limit 5""",
        # Reliability: installs whose `health` event reported brain != ok in the last 24h.
        "unhealthy": f"""
            select uniq(person_id) from events
            where event = 'health' and {w} and timestamp >= now() - interval 24 hour
              and coalesce(toString(properties.brain), '') not in ('', 'ok')""",
    }


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


# Which payload fields each query feeds. A failed query makes its fields None ("unknown"), never 0.
def derive(rows: dict[str, list], now: datetime, failed: frozenset[str] = frozenset()) -> dict:
    """Raw HogQL result rows (by query name) -> the JSON payload. Pure; no I/O.
    Fields fed by a query in ``failed`` are None; health / problem_raised fields are None until
    the event has been seen at all."""
    ok = lambda *names: not (failed & set(names))  # noqa: E731
    seen = {str(e) for e, _ in rows.get("seen_events", [])}
    seen_known = ok("seen_events")
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

    act_ok, ret_ok = ok("activation"), ok("activation", "retention")
    cohorts = []
    for wk in weeks:
        installs, activated = cohort_installs.get(wk, 0), cohort_activated.get(wk, 0)
        # Measurable once the cohort's last install has passed day 28: week start + 7 + 28 + 1 days.
        mature = date.fromisoformat(wk) + timedelta(days=7 + RETENTION_DAYS[1] + 1) <= today
        cohorts.append(
            {
                "week": wk,
                "installs": installs if act_ok else None,
                "activated": activated if act_ok else None,
                "activation_rate": _rate(activated, installs) if act_ok else None,
                "w4_retained": retained.get(wk, 0) if mature and ret_ok else None,
                "w4_retention": _rate(retained.get(wk, 0), installs) if mature and ret_ok else None,
                "activated_by_platform": {
                    p: platforms.get(wk, {}).get(p, {}).get("activated", 0) if act_ok else None for p in platform_names
                },
            }
        )

    tasks_ok = ok("tasks_weekly")
    funnel = dict((e, int(u)) for e, u in rows["funnel"])
    # Health / problem_raised may not be emitted yet: unseen (or unknowable) is None, not 0.
    health_ok = seen_known and "health" in seen and ok("unhealthy")
    problems_ok = seen_known and "problem_raised" in seen and ok("problem_kinds")
    return {
        "generated_at": now.isoformat(),
        "weeks": weeks,
        "failed_queries": sorted(failed),
        "wad": {
            "rolling_7d": (int(rows["doers_7d"][0][0]) if rows["doers_7d"] else 0) if ok("doers_7d") else None,
            "weekly": [{"week": wk, "doers": doers.get(wk, 0) if ok("doers_weekly") else None} for wk in weeks],
        },
        "platforms": platform_names,
        "cohorts": cohorts,
        "task_success": {
            "weekly": [
                {
                    "week": wk,
                    "completed": tasks.get(wk, {}).get("task_completed", 0) if tasks_ok else None,
                    "failed": tasks.get(wk, {}).get("task_failed", 0) if tasks_ok else None,
                    "rate": _rate(
                        tasks.get(wk, {}).get("task_completed", 0),
                        tasks.get(wk, {}).get("task_completed", 0) + tasks.get(wk, {}).get("task_failed", 0),
                    )
                    if tasks_ok
                    else None,
                }
                for wk in weeks
            ],
            "top_failed_reasons": [{"key": str(k), "count": int(n)} for k, n in rows["failed_reasons"]]
            if ok("failed_reasons")
            else None,
            "top_problem_kinds": [{"key": str(k), "count": int(n)} for k, n in rows["problem_kinds"]]
            if problems_ok
            else None,
        },
        "funnel": [{"stage": st, "users": funnel.get(st, 0) if ok("funnel") else None} for st in FUNNEL_STAGES],
        "reliability": {
            "unhealthy_installs_24h": (int(rows["unhealthy"][0][0]) if rows["unhealthy"] else 0) if health_ok else None
        },
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
    failed = frozenset(e["domain"].split(":", 1)[1] for e in errors)
    data = derive({n: r["results"] for n, r in by_name.items()}, datetime.now(timezone.utc), failed)
    state = env.PARTIAL if errors else env.OK
    if not errors:
        _cache = (time.monotonic() + CACHE_TTL_S, data, state, errors)
    return data, state, errors
