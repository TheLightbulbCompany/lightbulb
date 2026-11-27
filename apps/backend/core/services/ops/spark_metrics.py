"""Spark product metrics for the admin console: HogQL run as saved PostHog endpoints, cached.

Events come from the desktop app via POST /os/event (core/observability/spark_events.py) and from
the server (subscription_*, ...). Every query excludes team accounts (``_internal_filter``) and other
environments, and is bounded by time. Usage queries also need an account (``_account_filter``). Not the install
list (``installs``: every first launch of a released app, signed in or not) nor the anonymous download clicks.

Identity: pre-sign-in events are under the install id, aliased to the Clerk user at sign-in, so
everything per-user keys on HogQL ``person_id``. Installs = persons with an ``app_first_launch`` that carries an app_version (a released app).
A "human task" = task_completed whose ``source`` is missing or != 'automation'. Weeks are UTC,
Monday start; the current week is flagged ``partial``.

A failed query degrades only the sections built from it: ``data["unavailable"]`` lists their top-level keys and
``compute`` returns the failed query names (PostHog unset -> data=None).
"""

from __future__ import annotations

import asyncio
import math
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from core.config import settings
from core.services import posthog_admin
from core.services.ops import envelope as env
from core.services.ops.growth import _hogql_filter, _monday, _parse_ts, _utc_date, _weeks

WEEKS = 12
INSTALL_DAYS = 84  # install scan window (12 weeks); event scans use 90d so day-29 follow-ups fit
OK_TTL_S, FAIL_TTL_S = 300, 60
# Each query is a saved PostHog endpoint (posthog_admin.endpoint), recomputed by PostHog every 15 minutes and answered
# from the stored result. One that is not computed yet (just published, or its computation failed) runs live, and
# PostHog runs 3 live queries at once per project and makes the rest wait (posthog.com/docs/api/queries, read
# 2026-11-26): sent all together, the waiting ones used up their client timeout in PostHog's line and their sections
# showed "couldn't load" although every query finished. So we send 3 at a time and a query's timeout covers only its
# own run (PostHog caps a run at 10s). BUDGET_S bounds the whole load: the API gateway in front of the backend gives up
# at 30 seconds (api-stack.ts) and then the page gets nothing, so a query still running at the budget is cut off and
# reported as a timeout, like one that could not start. That happens on the first load after a query changes, while
# its endpoints run live; the next load, a minute later, reads the stored results.
PARALLEL, QUERY_TIMEOUT_S, BUDGET_S = 3, 15.0, 25.0
INSTALL_CAP = 5000
BUCKETS = (("lt5m", 5), ("lt30m", 30), ("lt2h", 120), ("lt24h", 1440))  # minutes: launch -> first human task
STEP_ORDER = ("sign_in", "brain", "permissions", "say_hi")
D7_LABEL = "activated d7 (installs 7d+ old)"
STEPS = ("signin", "brain", "permission", "onboarding", "first task", "activated d7", "subscribed")

# Failed query -> the top-level payload keys built from it (see derive).
QUERY_KEYS: dict[str, tuple[str, ...]] = {
    "doers_weekly": ("wad",),
    "doers_7d": ("wad",),
    "installs": ("cohorts", "funnel", "channels", "people", "time_to_first_task", "heard_from", "truncated"),
    "success_weekly": ("success",),
    "success_version": ("success",),
    "success_provider": ("success",),
    "problems": ("success", "brain_unavailable_installs_24h"),
    "downloads": ("downloads",),
    "voice_monthly": ("money",),
    "subs": ("money",),
    "setup_steps": ("setup",),
    "tasks_unfinished": ("setup",),
    "signals": ("signals",),
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_UTC_DAY = "toDate(toTimeZone(timestamp, 'UTC'))"
_UTC_WEEK = "toStartOfWeek(toTimeZone(timestamp, 'UTC'), 1)"


def _internal_filter() -> str:
    """Exclude team accounts: every person (alias-resolved, so their pre-sign-in install events too)
    that owns a Clerk id in SPARK_INTERNAL_USER_IDS / PLATFORM_ADMIN_USER_IDS."""
    ids = sorted(i for i in settings.spark_internal_user_ids if _SAFE_ID.match(i))
    if not ids:
        return "1 = 1"
    id_list = ", ".join(repr(i) for i in ids)
    return f"person_id not in (select person_id from person_distinct_ids where distinct_id in ({id_list}))"


def _account_filter() -> str:
    """Count only people with an account: their person owns a Clerk user id (``user_...``, the distinct id the
    backend captures signed-in events under, routers/os.py post_event). An install that never signed in is a
    person with only its random uuid, so it is not counted; once the person signs in, the backend aliases the
    install id onto the account and the earlier events count."""
    return "person_id in (select person_id from person_distinct_ids where startsWith(distinct_id, 'user_'))"


def _release_install() -> str:
    """A first launch of a RELEASED app: released apps always send app_version, an engine run from source does not.
    app_first_launch is one event per installation, so this is an install-level rule."""
    return "coalesce(toString(properties.app_version), '') != ''"


def _human(p: str = "") -> str:
    return f"{p}event = 'task_completed' and coalesce(toString({p}properties.source), '') != 'automation'"


def build_queries() -> dict[str, str]:
    w = f"{_hogql_filter(settings.ENVIRONMENT or '')} and {_internal_filter()}"
    wa = f"{w} and {_account_filter()}"  # usage queries need an account; `installs` and `downloads` do not
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
              where {human} and {wa} and timestamp >= now() - interval {WEEKS} week
              group by wk, person_id having uniq({_UTC_DAY}) >= 2)
            group by wk limit 100""",
        # Doers, rolling: same definition over the last 7 days (period 'last7') and the 7 before ('prev7').
        "doers_7d": f"""
            select period, count() from (
              select person_id, if(timestamp >= now() - interval 7 day, 'last7', 'prev7') as period from events
              where {human} and {wa} and timestamp >= now() - interval 14 day
              group by person_id, period having uniq({_UTC_DAY}) >= 2)
            group by period limit 10""",
        # One row per install-person (launch in the last 84d), newest first. Funnel, cohorts, channels and
        # the people table are all derived from it in Python. Every event counts only at/after the person's
        # launch (`after`). Columns: launch, platform, first app_version, brain provider (latest), channel,
        # email, then: has_account (signed in = the person owns a Clerk id, whatever the event order), counts: brain_chosen, accessibility permission, onboarding, human tasks,
        # human tasks within 24h of launch (activated_24h), distinct UTC days with a human task within 7d of
        # launch (activated_d7 = >= 2), human tasks on days 22-28 after launch (week 4), spark subscription,
        # the last human-task UTC day, the first human task after launch (valid only when human tasks > 0),
        # onboarding_step permissions completed (either platform), and spark_heard_from.
        "installs": f"""
            select l.launch, l.platform, l.version,
              argMaxIf(toString(e.properties.provider), e.timestamp, e.event = 'brain_chosen' and {after}),
              p.channel, coalesce(p.email, ''),
              l.has_account,
              countIf(e.event = 'brain_chosen' and {after}),
              countIf(e.event = 'permission_granted' and toString(e.properties.permission) = 'accessibility' and {after}),
              countIf(e.event = 'onboarding_completed' and {after}),
              countIf({h} and {after}),
              countIf({h} and {after} and e.timestamp <= l.launch + interval 24 hour),
              uniqIf({e_day}, {h} and {after} and e.timestamp < l.launch + interval 7 day),
              countIf({h} and e.timestamp >= l.launch + interval 22 day and e.timestamp < l.launch + interval 29 day),
              countIf(e.event = 'subscription_started' and toString(e.properties.product) = 'spark' and {after}),
              toString(maxIf({e_day}, {h} and {after})),
              minIf(e.timestamp, {h} and {after}),
              countIf(e.event = 'onboarding_step' and toString(e.properties.step) = 'permissions'
                      and toString(e.properties.action) = 'completed' and {after}),
              p.heard
            from (select person_id, min(timestamp) as launch,
                    argMin({plat}, timestamp) as platform, argMin({ver}, timestamp) as version,
                    if({_account_filter()}, 1, 0) as has_account
                  from events where event = 'app_first_launch' and {w} and {_release_install()}
                  group by person_id having launch >= now() - interval {INSTALL_DAYS} day) as l
            left join (select id, toString(properties.email) as email,
                         coalesce(nullIf(toString(properties.spark_channel), ''),
                                  nullIf(toString(properties.$initial_utm_source), ''), 'unknown') as channel,
                         coalesce(nullIf(toString(properties.spark_heard_from), ''), 'not answered') as heard
                       from persons) as p on p.id = l.person_id
            left join (select person_id, event, timestamp, properties from events
                       where event in ('brain_chosen', 'permission_granted', 'onboarding_completed',
                                       'task_completed', 'subscription_started', 'onboarding_step')
                         and {w} and timestamp >= now() - interval 90 day
                         and (event not in ('task_completed', 'subscription_started') or {_account_filter()})) as e
              on l.person_id = e.person_id
            group by l.person_id, l.launch, l.platform, l.version, l.has_account, p.channel, p.email, p.heard
            order by l.launch desc limit {INSTALL_CAP}""",
        # Task success = human task_completed / (human task_completed + task_failed): weekly by platform,
        # by app_version (30d) and by brain provider (30d; latest brain_chosen per person, any age <= 180d).
        "success_weekly": f"""
            select {_UTC_WEEK} as wk, {plat} as k, {kn} from events
            where {tasks} and {wa} and timestamp >= now() - interval {WEEKS} week group by wk, k limit 500""",
        "success_version": f"""
            select {ver} as k, {kn} from events
            where {tasks} and {wa} and timestamp >= now() - interval 30 day group by k limit 500""",
        "success_provider": f"""
            select coalesce(nullIf(p.provider, ''), 'unknown') as k, countIf({_human("t.")}), countIf(t.event = 'task_failed')
            from (select person_id, event, properties from events where {tasks} and {wa}
                  and timestamp >= now() - interval 30 day) as t
            left join (select person_id, argMax(toString(properties.provider), timestamp) as provider from events
                       where event = 'brain_chosen' and {wa} and timestamp >= now() - interval 180 day
                       group by person_id) as p on t.person_id = p.person_id
            group by k limit 50""",
        # Last 7d: task_failed by reason and problem_raised by kind, with the distinct installs
        # (properties.install_id) that raised each in the last 24h (brain.unavailable -> headline).
        "problems": f"""
            select event, coalesce(nullIf(toString(if(event = 'task_failed', properties.reason, properties.kind)), ''), 'unknown') as k,
              count(), uniqIf(toString(properties.install_id), timestamp >= now() - interval 24 hour)
            from events where event in ('task_failed', 'problem_raised') and {wa} and timestamp >= now() - interval 7 day
            group by event, k order by count() desc limit 200""",
        # Last 30d: spark_download_clicked (anonymous web traffic, never a funnel step) by source / placement.
        "downloads": f"""
            select coalesce(nullIf(toString(properties.source), ''), nullIf(toString(properties.placement), ''), 'unknown') as k, count()
            from events where event = 'spark_download_clicked' and {w} and timestamp >= now() - interval 30 day
            group by k order by count() desc limit 20""",
        # Voice per person and UTC month (6 months): seconds = sum(voice_session_ended.seconds), sessions.
        # Minutes = seconds / 60; cost = minutes x SPARK_VOICE_USD_PER_MIN (an estimate).
        "voice_monthly": f"""
            select toString(toStartOfMonth(toTimeZone(v.timestamp, 'UTC'))) as m, v.person_id,
              sum(toInt(v.properties.seconds)), count(), coalesce(p.email, '')
            from (select person_id, timestamp, properties from events where event = 'voice_session_ended'
                  and toString(properties.product) = 'spark' and {wa} and timestamp >= now() - interval 6 month) as v
            left join (select id, toString(properties.email) as email from persons) as p on p.id = v.person_id
            group by m, v.person_id, p.email limit 20000""",
        # Money facts per person, oldest first (12 months): the latest subscription_updated.status_to is the
        # status; no update but a subscription_started = unknown (data before the updates existed); the month
        # is the UTC month of the event. Trial -> paid and cancellations are derived from these rows.
        "subs": f"""
            select person_id, event, toString(toStartOfMonth(toTimeZone(timestamp, 'UTC'))),
              coalesce(toString(properties.status_to), ''), coalesce(toString(properties.reason), ''),
              coalesce(toString(properties.feedback), '')
            from events
            where event in ('account_created', 'subscription_started', 'subscription_updated', 'subscription_cancelled')
              and toString(properties.product) = 'spark'
              and {wa} and timestamp >= now() - interval 12 month
            order by timestamp limit 50000""",
        # Setup, last 30d: distinct persons by event, key (step / reason / permission) and action.
        "setup_steps": f"""
            select event,
              coalesce(nullIf(toString(multiIf(event = 'onboarding_step', properties.step,
                event = 'brain_signin_failed', properties.reason, properties.permission)), ''), 'unknown') as k,
              if(event = 'onboarding_step', coalesce(toString(properties.action), ''), '') as a, uniq(person_id)
            from events
            where event in ('onboarding_step', 'brain_signin_failed', 'permission_prompted', 'permission_denied')
              and {wa} and timestamp >= now() - interval 30 day
            group by event, k, a limit 200""",
        # Last 30d, tasks started (not automation) >= 30 min ago: n; those with no task_completed/task_failed
        # or task_stopped carrying the same task_id: k. One row: (k, n).
        "tasks_unfinished": f"""
            select countIf(fin = 0), count() from (
              select toString(properties.task_id) as tid,
                countIf(event = 'task_started' and coalesce(toString(properties.source), '') != 'automation') as st,
                minIf(timestamp, event = 'task_started') as first_start,
                countIf(event in ('task_completed', 'task_failed', 'task_stopped')) as fin
              from events
              where event in ('task_started', 'task_completed', 'task_failed', 'task_stopped')
                and {wa} and timestamp >= now() - interval 30 day and toString(properties.task_id) != ''
              group by tid having st > 0 and first_start < now() - interval 30 minute) limit 1""",
        # Last 30d, one row per (event, key): event, key, count, distinct persons, persons in the last 7d, persons
        # with full access (voice only). Key: task_stopped.how, automation_fired '<ok>/<brain>', feature_used.feature,
        # voice_session_ended.kind, app_opened/update_installed version, else '-'. task_started = human tasks (n of
        # the stop share); problem_raised only kind crash. The first half adds one exact TOTAL row (key '*') for
        # automation_fired and voice_session_ended: distinct people cannot be summed across keys.
        # derive() turns the rows into the `signals` payload.
        "signals": f"""
            select event, '*' as k, count(), uniq(person_id),
              uniqIf(person_id, timestamp >= now() - interval 7 day),
              uniqIf(person_id, toString(properties.full_access) = 'true')
            from events
            where event in ('automation_fired', 'voice_session_ended')
              and toString(properties.product) = 'spark' and {wa} and timestamp >= now() - interval 30 day
            group by event
            union all
            select event,
              coalesce(nullIf(toString(multiIf(
                event = 'task_stopped', properties.how,
                event = 'automation_fired', concat(toString(properties.ok), '/', toString(properties.brain)),
                event = 'feature_used', properties.feature, event = 'voice_session_ended', properties.kind,
                event = 'app_opened', properties.app_version, event = 'update_installed', properties.to_version, '')), ''), '-') as k,
              count(), uniq(person_id),
              uniqIf(person_id, timestamp >= now() - interval 7 day),
              uniqIf(person_id, toString(properties.full_access) = 'true')
            from events
            where (event in ('task_started', 'task_stopped', 'automation_fired', 'feature_used',
                             'voice_session_ended', 'app_opened', 'update_installed')
                   or (event = 'problem_raised' and toString(properties.kind) = 'crash'))
              and (event != 'task_started' or coalesce(toString(properties.source), '') != 'automation')
              and toString(properties.product) = 'spark' and {wa} and timestamp >= now() - interval 30 day
            group by event, k limit 500""",
    }


def _money(rows: dict[str, list], now: datetime) -> dict:
    """Subscription facts and voice usage. A part is None when its events were never seen ("no data yet")."""
    price = settings.SPARK_PRICE_USD
    rate = getattr(settings, "SPARK_VOICE_USD_PER_MIN", 0.05)
    status, started, created, ever_active, cancels, first_upd = {}, set(), {}, set(), {}, ""
    this = now.strftime("%Y-%m")
    for person, event, month, to, reason, feedback in rows["subs"]:
        month = month[:7]
        if event == "account_created":
            created.setdefault(person, month)
        elif event == "subscription_started":
            started.add(person)
        elif event == "subscription_updated":
            status[person] = to or "other"
            first_upd = first_upd or month  # rows are oldest first
            if to == "active":
                ever_active.add(person)
        elif event == "subscription_cancelled":
            c = cancels.setdefault(month, [0, Counter(), Counter()])
            c[0] += 1
            c[1][reason or "unknown"] += 1
            c[2][feedback or "none"] += 1
    subs = None
    if status:  # no subscription_updated yet = paying cannot be told: no figures, not zeros
        paying = sum(s == "active" for s in status.values())
        by_month = {}
        for person, month in created.items():
            if month < first_upd:  # accounts before status updates existed cannot show their first charge
                continue
            b = by_month.setdefault(month, [0, 0])
            b[0] += person in ever_active
            b[1] += 1
        subs = {
            "paying": paying,
            "mrr_usd": paying * price,
            "unknown": len(started - set(status)),
            # partial: the month of the first status update (earlier conversions are invisible) and this month
            "trial_to_paid": [
                {"month": m, "k": k, "n": n, "partial": m in (first_upd, this)}
                for m, (k, n) in sorted(by_month.items())
            ],
            "cancellations": [
                {
                    "month": m,
                    "count": c[0],
                    "reasons": [{"key": k, "count": n} for k, n in c[1].most_common()],
                    "feedback": [{"key": k, "count": n} for k, n in c[2].most_common()],
                }
                for m, c in sorted(cancels.items())
            ],
        }
    voice = None
    if rows["voice_monthly"]:
        months: dict[str, dict[str, float]] = {}
        emails = {}
        for m, person, secs, _, email in rows["voice_monthly"]:
            months.setdefault(m[:7], {})[person] = float(secs) / 60
            emails[person] = email
        label = lambda p: status.get(p) or ("unknown" if p in started else "none")  # noqa: E731
        voice = {
            "rate_usd_per_min": rate,
            "months": [
                {
                    "month": m,
                    "minutes": round(sum(u.values()), 1),
                    "users": len(u),
                    "p50": round(statistics.median(u.values()), 1),
                    "p90": round(sorted(u.values())[math.ceil(0.9 * len(u)) - 1], 1),
                    "cost_usd": round(sum(u.values()) * rate, 2),
                }
                for m, u in sorted(months.items())
            ],
            "top": [
                {
                    "email": emails[p] or None,
                    "status": label(p),
                    "minutes": round(mins, 1),
                    "cost_usd": round(mins * rate, 2),
                    "margin_usd": round((price if label(p) == "active" else 0) - mins * rate, 2),
                }
                for p, mins in sorted(months.get(this, {}).items(), key=lambda x: -x[1])[:20]
            ],
        }
    return {"price_usd": price, "subs": subs, "voice": voice}


def _kn(k: int, n: int) -> dict:
    return {"k": k, "n": n}


def _setup(rows: dict[str, list]) -> dict | None:
    """Onboarding losses, sign-in failures, permission denials (distinct persons, 30d), tasks never finished.
    None = no onboarding_step ever seen (the app that sends them is not out yet)."""
    seen = {(e, str(k), a): int(n) for e, k, a, n in rows["setup_steps"]}
    if not any(e == "onboarding_step" for e, _, _ in seen):
        return None
    step = lambda s, a: seen.get(("onboarding_step", s, a), 0)  # noqa: E731
    stuck = rows["tasks_unfinished"][0] if rows["tasks_unfinished"] else (0, 0)
    return {
        "steps": [
            {
                "step": s,
                "shown": step(s, "shown"),
                "completed": _kn(step(s, "completed"), step(s, "shown")),
                "skipped": _kn(step(s, "skipped"), step(s, "shown")),
            }
            for s in STEP_ORDER
        ],
        "signin_failures": [
            {"key": k, "count": n}
            for (e, k, _), n in sorted(seen.items(), key=lambda x: -x[1])
            if e == "brain_signin_failed"
        ],
        "permissions": [
            {"permission": p, "prompted": n, "denied": _kn(seen.get(("permission_denied", p, ""), 0), n)}
            for (e, p, _), n in sorted(seen.items())
            if e == "permission_prompted"
        ],
        "unfinished": _kn(int(stuck[0]), int(stuck[1])) if int(stuck[1]) else None,
    }


LABELS = {
    "hold": "push to talk",
    "handsFree": "hands-free",
    "wakeWord": "wake word",
    "typed": "typing",
    "automation_created": "created an automation",
    "-": "unknown",  # a missing value
}


class _Row(NamedTuple):
    count: int
    people_30d: int
    people_7d: int
    full_access: int


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())  # numbers, not strings: 2.1.10 > 2.1.9


def _signals(rows: list) -> dict:
    """Stops, automations, features, crashes and versions from the `signals` rows (30d). Each part is None
    until its events were seen ("no data yet"), never a table of zeros. Counts only; shares are k and n."""
    by: dict[str, dict[str, _Row]] = {}
    for event, k, *nums in rows:
        by.setdefault(event, {})[str(k)] = _Row(*(int(x) for x in nums))
    # exact distinct people per event (key '*'); the per-key rows only feed the breakdowns
    star = {e: keys.pop("*") for e, keys in by.items() if "*" in keys}
    total = lambda e, f="count": sum(getattr(r, f) for r in by.get(e, {}).values())  # noqa: E731

    stopped = None
    if "task_stopped" in by:
        k = total("task_stopped")
        how = {h: by["task_stopped"].get(h, _Row(0, 0, 0, 0)).count for h in ("pressed", "said")}
        stopped = {
            "k": k,
            "n": total("task_started"),
            # a missing or unknown how is "other", so the three always add up to k
            "how": [
                *({"key": h, "count": c} for h, c in how.items()),
                {"key": "other", "count": k - sum(how.values())},
            ],
        }
    automations = None
    if "automation_fired" in star:
        runs = star["automation_fired"].count
        fired = by["automation_fired"]
        automations = {
            "runs": runs,
            "ok": _kn(sum(r.count for k, r in fired.items() if k.startswith("true/")), runs),
            "with_brain": _kn(sum(r.count for k, r in fired.items() if k.endswith("/true")), runs),
            "people_30d": star["automation_fired"].people_30d,
        }
    feats = [
        {"feature": LABELS.get(k, k), "people_30d": r.people_30d, "people_7d": r.people_7d}
        for e in ("voice_session_ended", "feature_used")
        for k, r in by.get(e, {}).items()
    ]
    if full := star["voice_session_ended"].full_access if "voice_session_ended" in star else 0:
        feats.append({"feature": "full access", "people_30d": full, "people_7d": None})
    if automations:
        feats.append(
            {
                "feature": "automation ran",
                "people_30d": automations["people_30d"],
                "people_7d": star["automation_fired"].people_7d,
            }
        )
    opened = sorted(
        ((LABELS.get(k, k), r.people_7d) for k, r in by.get("app_opened", {}).items()), key=lambda x: (-x[1], x[0])
    )
    updates = sorted(by.get("update_installed", {}).items(), key=lambda x: _version_key(x[0]), reverse=True)
    return {
        "stopped": stopped,
        "automations": automations,
        "features": sorted(feats, key=lambda f: (-f["people_30d"], f["feature"])) or None,
        # about: a person who updated during the week is in two versions
        "crash": _kn(total("problem_raised", "people_7d"), sum(p for _, p in opened)) if opened else None,
        "versions": [{"version": k, "people_7d": p} for k, p in opened] or None,
        "updates": [{"to_version": LABELS.get(k, k), "installs_30d": r.count} for k, r in updates] or None,
    }


def derive(rows: dict[str, list], now: datetime) -> dict:
    """Raw HogQL rows (by query name) -> the JSON payload. Pure; no I/O. Counts only; every share is k and n."""
    weeks = _weeks(now, WEEKS)
    cur = weeks[-1]
    wk_of = lambda v: str(v)[:10]  # noqa: E731  PostHog returns dates as ISO strings
    kn = lambda k, n: {"k": k, "n": n}  # noqa: E731

    per7 = {p: int(n) for p, n in rows["doers_7d"]}
    doers = {wk_of(w): int(n) for w, n in rows["doers_weekly"]}

    people, cohorts, channels, heard = [], {}, {}, {}
    ttft = [0] * (len(BUCKETS) + 2)  # buckets, later, never
    ttft_n = 0
    funnel = [0] * (len(STEPS) + 1)  # [launches, then STEPS]
    d7_base = 0  # installs 7d+ old (30d window): the denominator of the d7 step
    for r in rows["installs"]:
        launch = _parse_ts(r[0])
        platform, version, provider, channel, email = r[1], r[2], r[3] or "unknown", r[4], r[5]
        signin, brain, perm, onb, tasks, act24, d7days, w4, sub = (int(x) for x in r[6:15])
        last_task = str(r[15])[:10] if tasks else None
        first_task, perm_step, hf = r[16], int(r[17]), r[18] or "not answered"
        # Windows has nothing to grant: it reached the permissions step once it reached the brain step.
        perm = perm > 0 or perm_step > 0 or (platform == "windows" and brain > 0)
        age = now - launch
        settled, d7_old = age >= timedelta(hours=24), age >= timedelta(days=7)
        d7 = d7_old and d7days >= 2
        reached = [signin, brain, perm, onb, tasks, d7, sub]
        reached = [bool(x) for x in reached]
        if age <= timedelta(days=30):
            if settled:
                ttft_n += 1
                mins = (_parse_ts(first_task) - launch).total_seconds() / 60 if tasks else None
                ttft[
                    next(
                        (i for i, (_, m) in enumerate(BUCKETS) if mins is not None and mins < m),
                        len(BUCKETS) + (mins is None),
                    )
                ] += 1
            funnel[0] += 1
            d7_base += d7_old
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
            h = heard.setdefault(hf, [0, 0, 0])  # [activated, settled, installs]
            h[2] += 1
            if settled:
                c = channels.setdefault(channel, [0, 0])
                c[0] += act24 > 0
                c[1] += 1
                h[0] += act24 > 0
                h[1] += 1
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
        "funnel": [
            {"step": s, "k": k, "n": funnel[0]} if s != "activated d7" else {"step": D7_LABEL, "k": k, "n": d7_base}
            for s, k in zip(("first launch", *STEPS), funnel)
        ],
        "truncated": len(rows["installs"]) >= INSTALL_CAP,
        "downloads": [{"key": str(k), "count": int(n)} for k, n in rows["downloads"]],
        "channels": [
            {"channel": c, "activated_24h": kn(k, n)} for c, (k, n) in sorted(channels.items(), key=lambda x: -x[1][1])
        ],
        "people": people,
        "money": _money(rows, now),
        "time_to_first_task": [
            {"bucket": b, "k": k, "n": ttft_n} for b, k in zip([*(b for b, _ in BUCKETS), "later", "never"], ttft)
        ],
        # None = no install has answered yet (never seen), not a table of zeros.
        "heard_from": None
        if set(heard) <= {"not answered"}
        else [
            {"choice": c, "installs": i, "activated_24h": kn(a, n)}
            for c, (a, n, i) in sorted(heard.items(), key=lambda x: -x[1][2])
        ],
        "setup": _setup(rows),
        "signals": _signals(rows["signals"]),
    }


_cache: tuple[float, dict | None, str, list[dict]] | None = None  # (expires_at, data, state, errors)


async def compute() -> tuple[dict | None, str, list[dict]]:
    """Run every query. A failed query -> its sections are listed in data["unavailable"] (built from empty rows, never shown).
    Failures are cached too (60s) so an open page during a PostHog outage doesn't hammer the API."""
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    queries = build_queries()
    gate, deadline = asyncio.Semaphore(PARALLEL), time.monotonic() + BUDGET_S

    async def run(n: str, q: str) -> dict:
        late = {"columns": [], "results": [], "as_of": None, "stubbed": False, "error": "timeout"}
        async with gate:
            left = deadline - time.monotonic()
            if left <= 0:  # not sent: it could not finish inside the request
                return late
            stem = f"spark_{settings.ENVIRONMENT}_{n}"  # dev and prod share one PostHog project
            call = posthog_admin.endpoint(stem, q, timeout_s=min(QUERY_TIMEOUT_S, left))
            try:  # httpx's timeout is per phase and a publish is several requests: this is the bound on the whole call
                return await asyncio.wait_for(call, left)
            except asyncio.TimeoutError:
                return late

    results = await asyncio.gather(*(run(n, q) for n, q in queries.items()))
    if all(r["stubbed"] for r in results):
        return None, env.UNAVAILABLE, [{"domain": "posthog", "reason": "not configured"}]
    errors = [{"domain": f"posthog:{n}", "reason": r["error"]} for n, r in zip(queries, results) if r["error"]]
    rows = {n: [] if r["error"] else r["results"] for n, r in zip(queries, results)}
    data = derive(rows, datetime.now(timezone.utc))
    computed = [_parse_ts(r["as_of"]) for r in results if r.get("as_of")]
    if computed:  # the page says "Generated <this>": the oldest of PostHog's computations, not the time of this request
        data["generated_at"] = min(computed).isoformat()
    data["unavailable"] = sorted({k for n, r in zip(queries, results) if r["error"] for k in QUERY_KEYS[n]})
    state = env.PARTIAL if errors else env.OK
    _cache = (time.monotonic() + (FAIL_TTL_S if errors else OK_TTL_S), data, state, errors)
    return data, state, errors
