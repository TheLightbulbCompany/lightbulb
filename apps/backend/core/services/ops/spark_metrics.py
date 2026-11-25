"""Spark product metrics for the admin console: HogQL over PostHog, cached.

Events come from the desktop app via POST /os/event (core/observability/spark_events.py) and from
the server (subscription_*, ...). Every query excludes team accounts (``_internal_filter``) and other
environments, and is bounded by time.

Identity: pre-sign-in events are under the install id, aliased to the Clerk user at sign-in, so
everything per-user keys on HogQL ``person_id``. Installs = persons with ``app_first_launch``.
A "human task" = task_completed whose ``source`` is missing or != 'automation'. Weeks are UTC,
Monday start; the current week is flagged ``partial``.

Any failed query -> no figures at all (``compute`` returns data=None + the failed query names).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.services import posthog_admin
from core.services.ops import envelope as env
from core.services.ops.growth import _hogql_filter, _monday, _parse_ts, _utc_date, _weeks

WEEKS = 12
INSTALL_DAYS = 84  # install scan window (12 weeks); event scans use 90d so day-29 follow-ups fit
OK_TTL_S, FAIL_TTL_S = 300, 60
STEPS = ("signin", "brain", "permission", "onboarding", "first task", "activated d7", "subscribed")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_UTC_DAY = "toDate(toTimeZone(timestamp, 'UTC'))"
_UTC_WEEK = "toStartOfWeek(toTimeZone(timestamp, 'UTC'), 1)"
_CHANNEL = (
    "coalesce(nullIf(toString(person.properties.spark_channel), ''),"
    " nullIf(toString(person.properties.$initial_utm_source), ''), 'unknown')"
)


def _internal_filter() -> str:
    """Exclude team accounts: every person (alias-resolved, so their pre-sign-in install events too)
    that owns a Clerk id in SPARK_INTERNAL_USER_IDS / PLATFORM_ADMIN_USER_IDS."""
    ids = sorted(i for i in settings.spark_internal_user_ids if _SAFE_ID.match(i))
    if not ids:
        return "1 = 1"
    id_list = ", ".join(repr(i) for i in ids)
    return f"person_id not in (select person_id from person_distinct_ids where distinct_id in ({id_list}))"


def _human(p: str = "") -> str:
    return f"{p}event = 'task_completed' and coalesce(toString({p}properties.source), '') != 'automation'"


def build_queries() -> dict[str, str]:
    w = f"{_hogql_filter(settings.ENVIRONMENT or '')} and {_internal_filter()}"
    human = _human()
    plat = "coalesce(nullIf(toString(properties.platform), ''), 'unknown')"
    ver = "coalesce(nullIf(toString(properties.app_version), ''), 'unknown')"
    tasks = "event in ('task_completed', 'task_failed')"
    kn = f"countIf({human}) as done, countIf(event = 'task_failed') as failed"
    h = _human("e.")
    after = "e.timestamp >= l.launch"
    e_day = "toDate(toTimeZone(e.timestamp, 'UTC'))"
    return {
        # Doers, weekly: persons with a human task on >= 2 distinct UTC days in the UTC week.
        "doers_weekly": f"""
            select wk, count() from (
              select {_UTC_WEEK} as wk, person_id from events
              where {human} and {w} and timestamp >= now() - interval {WEEKS} week
              group by wk, person_id having uniq({_UTC_DAY}) >= 2)
            group by wk limit 100""",
        # Doers, rolling: same definition over the last 7 days (period 'last7') and the 7 before ('prev7').
        "doers_7d": f"""
            select period, count() from (
              select person_id, if(timestamp >= now() - interval 7 day, 'last7', 'prev7') as period from events
              where {human} and {w} and timestamp >= now() - interval 14 day
              group by person_id, period having uniq({_UTC_DAY}) >= 2)
            group by period limit 10""",
        # One row per install-person (launch in the last 84d), newest first. Funnel, cohorts, channels and
        # the people table are all derived from it in Python. Every event counts only at/after the person's
        # launch (`after`). Columns: launch, platform, first app_version, brain provider (latest), channel,
        # email, then counts: signin, brain_chosen, accessibility permission, onboarding, human tasks,
        # human tasks within 24h of launch (activated_24h), distinct UTC days with a human task within 7d of
        # launch (activated_d7 = >= 2), human tasks on days 22-28 after launch (week 4), spark subscription,
        # and the last human-task UTC day.
        "installs": f"""
            select l.launch, l.platform, l.version,
              argMaxIf(toString(e.properties.provider), e.timestamp, e.event = 'brain_chosen' and {after}),
              l.channel, l.email,
              countIf(e.event = 'signin_completed' and {after}),
              countIf(e.event = 'brain_chosen' and {after}),
              countIf(e.event = 'permission_granted' and toString(e.properties.permission) = 'accessibility' and {after}),
              countIf(e.event = 'onboarding_completed' and {after}),
              countIf({h} and {after}),
              countIf({h} and {after} and e.timestamp <= l.launch + interval 24 hour),
              uniqIf({e_day}, {h} and {after} and e.timestamp < l.launch + interval 7 day),
              countIf({h} and e.timestamp >= l.launch + interval 22 day and e.timestamp < l.launch + interval 29 day),
              countIf(e.event = 'subscription_started' and toString(e.properties.product) = 'spark' and {after}),
              toString(maxIf({e_day}, {h} and {after}))
            from (select person_id, min(timestamp) as launch,
                    argMin({plat}, timestamp) as platform, argMin({ver}, timestamp) as version,
                    any({_CHANNEL}) as channel, any(coalesce(toString(person.properties.email), '')) as email
                  from events where event = 'app_first_launch' and {w} and timestamp >= now() - interval {INSTALL_DAYS} day
                  group by person_id) as l
            left join (select person_id, event, timestamp, properties from events
                       where event in ('signin_completed', 'brain_chosen', 'permission_granted', 'onboarding_completed',
                                       'task_completed', 'subscription_started')
                         and {w} and timestamp >= now() - interval 90 day) as e on l.person_id = e.person_id
            group by l.person_id, l.launch, l.platform, l.version, l.channel, l.email
            order by l.launch desc limit 5000""",
        # Task success = human task_completed / (human task_completed + task_failed): weekly by platform,
        # by app_version (30d) and by brain provider (30d; latest brain_chosen per person, any age <= 180d).
        "success_weekly": f"""
            select {_UTC_WEEK} as wk, {plat} as k, {kn} from events
            where {tasks} and {w} and timestamp >= now() - interval {WEEKS} week group by wk, k limit 500""",
        "success_version": f"""
            select {ver} as k, {kn} from events
            where {tasks} and {w} and timestamp >= now() - interval 30 day group by k limit 500""",
        "success_provider": f"""
            select coalesce(nullIf(p.provider, ''), 'unknown') as k, countIf({_human("t.")}), countIf(t.event = 'task_failed')
            from (select person_id, event, properties from events where {tasks} and {w}
                  and timestamp >= now() - interval 30 day) as t
            left join (select person_id, argMax(toString(properties.provider), timestamp) as provider from events
                       where event = 'brain_chosen' and {w} and timestamp >= now() - interval 180 day
                       group by person_id) as p on t.person_id = p.person_id
            group by k limit 50""",
        # Last 7d: task_failed by reason and problem_raised by kind, with the distinct installs
        # (properties.install_id) that raised each in the last 24h (brain.unavailable -> headline).
        "problems": f"""
            select event, coalesce(nullIf(toString(if(event = 'task_failed', properties.reason, properties.kind)), ''), 'unknown') as k,
              count(), uniqIf(toString(properties.install_id), timestamp >= now() - interval 24 hour)
            from events where event in ('task_failed', 'problem_raised') and {w} and timestamp >= now() - interval 7 day
            group by event, k order by count() desc limit 200""",
        # Last 30d: spark_download_clicked (anonymous web traffic, never a funnel step) by source / placement.
        "downloads": f"""
            select coalesce(nullIf(toString(properties.source), ''), nullIf(toString(properties.placement), ''), 'unknown') as k, count()
            from events where event = 'spark_download_clicked' and {w} and timestamp >= now() - interval 30 day
            group by k order by count() desc limit 20""",
    }


def derive(rows: dict[str, list], now: datetime) -> dict:
    """Raw HogQL rows (by query name) -> the JSON payload. Pure; no I/O. Counts only; every share is k and n."""
    weeks = _weeks(now, WEEKS)
    cur = weeks[-1]
    wk_of = lambda v: str(v)[:10]  # noqa: E731  PostHog returns dates as ISO strings
    kn = lambda k, n: {"k": k, "n": n}  # noqa: E731

    per7 = {p: int(n) for p, n in rows["doers_7d"]}
    doers = {wk_of(w): int(n) for w, n in rows["doers_weekly"]}

    people, cohorts, channels = [], {}, {}
    funnel = [0] * (len(STEPS) + 1)  # [launches, then STEPS]
    for r in rows["installs"]:
        launch = _parse_ts(r[0])
        platform, version, provider, channel, email = r[1], r[2], r[3] or "unknown", r[4], r[5]
        signin, brain, perm, onb, tasks, act24, d7days, w4, sub = (int(x) for x in r[6:15])
        last_task = str(r[15])[:10] if tasks else None
        age = now - launch
        settled, d7_old = age >= timedelta(hours=24), age >= timedelta(days=7)
        d7 = d7_old and d7days >= 2
        reached = [signin, brain, perm, onb, tasks, d7, sub]
        reached = [bool(x) for x in reached]
        if age <= timedelta(days=30):
            funnel[0] += 1
            for i, ok in enumerate(reached):
                funnel[i + 1] += ok
        if age <= timedelta(days=60):
            if len(people) < 200:
                furthest = next((s for s, ok in zip(reversed(STEPS), reversed(reached)) if ok), "first launch")
                people.append(
                    {
                        "installed": _utc_date(launch).isoformat(),
                        "platform": platform,
                        "version": version,
                        "provider": provider,
                        "channel": channel,
                        "furthest": furthest,
                        "last_task": last_task,
                        "email": email or None,
                    }
                )
            if settled:
                c = channels.setdefault(channel, [0, 0])
                c[0] += act24 > 0
                c[1] += 1
        wk = _monday(_utc_date(launch)).isoformat()
        if wk in weeks:
            c = cohorts.setdefault(
                (wk, platform), {"installs": 0, "settled": 0, "act": 0, "d7_k": 0, "d7_n": 0, "w4_k": 0}
            )
            c["installs"] += 1
            c["settled"] += settled
            c["act"] += settled and act24 > 0
            c["d7_n"] += d7_old
            c["d7_k"] += d7
            c["w4_k"] += settled and act24 > 0 and w4 > 0  # W4 only among activated installs

    cohort_rows = []
    for (wk, platform), c in sorted(cohorts.items()):
        # Mature once the cohort's last install has passed day 29: week start + 7 + 29 days.
        mature = datetime.fromisoformat(wk).replace(tzinfo=timezone.utc) + timedelta(days=7 + 29) <= now
        cohort_rows.append(
            {
                "week": wk,
                "partial": wk == cur,
                "platform": platform,
                "installs": c["installs"],
                "settled": c["settled"],
                "activated_24h": kn(c["act"], c["settled"]),
                "activated_d7": kn(c["d7_k"], c["d7_n"]),
                "w4": kn(c["w4_k"], c["act"]) if mature else None,  # None = too early
            }
        )

    def success(name):
        return [{"key": str(k), "success": kn(int(d), int(d) + int(f))} for k, d, f in rows[name]]

    def top(event):
        return [{"key": str(k), "count": int(n)} for e, k, n, _ in rows["problems"] if e == event][:10]

    return {
        "generated_at": now.isoformat(),
        "wad": {
            "last7": per7.get("last7", 0),
            "prev7": per7.get("prev7", 0),
            "weekly": [{"week": w, "partial": w == cur, "doers": doers.get(w, 0)} for w in weeks],
        },
        "cohorts": cohort_rows,
        "success": {
            "weekly": [
                {
                    "week": wk_of(w),
                    "partial": wk_of(w) == cur,
                    "platform": str(k),
                    "success": kn(int(d), int(d) + int(f)),
                }
                for w, k, d, f in sorted(rows["success_weekly"], key=lambda r: str(r[0]))
            ],
            "by_version": success("success_version"),
            "by_provider": success("success_provider"),
            "failed_reasons": top("task_failed"),
            "problem_kinds": top("problem_raised"),
        },
        "brain_unavailable_installs_24h": sum(
            int(i) for e, k, _, i in rows["problems"] if e == "problem_raised" and k == "brain.unavailable"
        ),
        "funnel": [{"step": s, "k": k} for s, k in zip(("first launch", *STEPS), funnel)],
        "downloads": [{"key": str(k), "count": int(n)} for k, n in rows["downloads"]],
        "channels": [
            {"channel": c, "activated_24h": kn(k, n)} for c, (k, n) in sorted(channels.items(), key=lambda x: -x[1][1])
        ],
        "people": people,
    }


_cache: tuple[float, dict | None, str, list[dict]] | None = None  # (expires_at, data, state, errors)


async def compute() -> tuple[dict | None, str, list[dict]]:
    """Run every query. Any failure -> (None, partial, failed query names): no figures, no fake zeros.
    Failures are cached too (60s) so an open page during a PostHog outage doesn't hammer the API."""
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    queries = build_queries()
    results = await asyncio.gather(*(posthog_admin.hogql(q) for q in queries.values()))
    if all(r["stubbed"] for r in results):
        return None, env.UNAVAILABLE, [{"domain": "posthog", "reason": "not configured"}]
    errors = [{"domain": f"posthog:{n}", "reason": r["error"]} for n, r in zip(queries, results) if r["error"]]
    data = None if errors else derive({n: r["results"] for n, r in zip(queries, results)}, datetime.now(timezone.utc))
    state = env.PARTIAL if errors else env.OK
    _cache = (time.monotonic() + (FAIL_TTL_S if errors else OK_TTL_S), data, state, errors)
    return data, state, errors
