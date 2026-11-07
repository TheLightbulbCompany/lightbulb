"""Growth metrics for the admin console — computed on read.

Definitions are DECIDED in docs/growth/2026-08-17-blitzscaling-strategy.md §3,
which since the 2026-09-10 pivot adopts the business-metrics section of
``docs/superpowers/plans/2026-09-12-observability-audit.md`` §6 verbatim — read
either, they are the same five metrics. (The data-source research doc that
seeded this module has shipped and been deleted per the "Planning docs are
disposable" CLAUDE.md rule.)

The pivot to a Slack-like multiplayer workspace is why the pre-pivot activation
ladder and the "autonomous runs per owner per week" North Star are gone — along
with the routine/run reads that fed them (§6 explicitly ignores
``agent_run_completed`` volume: routines and the nightly CEO inflate it and it
rewards automation churn, not humans coming back). Five metrics:

1. **Activated workspace rate** — within 7 days of the workspace's trial start,
   its humans TOGETHER sent ≥5 room messages on ≥2 distinct UTC days AND ≥1 agent
   message was posted in one of its rooms.
2. **Weekly active workspaces** (≥1 human room message in the ISO week) and the
   **multiplayer share** (those with ≥2 distinct human senders).
3. **Answered rate + p50 reply time** — human messages with ≥1 dispatch target
   followed, in the same room, by an agent message within 120s.
4. **Paying workspaces** (``subscription_status == "active"``) and **trial → paid
   by signup-week cohort**.
5. **Week-4 retention** — activated workspaces with ≥1 human message in the 4th
   ISO week after activation.

Sources: DynamoDB (billing rows; the messaging table via a paginated scan),
PostHog HogQL (the client acquisition funnel — never a headline), CloudWatch
(gate-blocked codes, work-DLQ depth).

Everything is segmented internal/external BEFORE aggregation.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from core.config import settings
from core.services.ops import envelope as env

logger = logging.getLogger(__name__)

WINDOW_WEEKS = 8
WINDOW_DAYS = WINDOW_WEEKS * 7  # 56
PAYING_STATUS = "active"  # §6 metric 4 — "paying workspaces" is exactly this status

# §6 metric 1 — both conditions, inside one window that opens at the trial start.
ACTIVATION_WINDOW_DAYS = 7
ACTIVATION_MIN_MESSAGES = 5
ACTIVATION_MIN_DAYS = 2
# §6 metric 3 — "answered within 2 min".
ANSWER_WINDOW_S = 120
# §6 metric 5 — the 4th ISO week AFTER the activation week.
RETENTION_WEEKS = 4

# --- HogQL: the acquisition funnel only. Its stages are landing_viewed ->
# signup_started -> trial_started -> activated, and the last one is SERVER-side
# (computed above from messages), deliberately: the client event for it does not
# exist and a client funnel is not allowed to define activation. ---
FUNNEL_EVENTS = ("landing_viewed", "signup_started", "trial_started")
_FUNNEL_EVENT_SQL = ", ".join(f"'{e}'" for e in FUNNEL_EVENTS)


def _hogql_filter(environment: str) -> str:
    """The shared filter (09-analytics §6): this backend's own environment only (dev and prod
    share one PostHog project), and on prod only the product hosts -- never the launch site
    or a dev host. Server events carry no ``$host`` and pass the host test."""
    name = "prod" if environment.strip().lower() in ("prod", "production") else (environment.strip() or "local")
    clause = f"properties.environment = '{name}'"
    if name == "prod":
        clause += (
            " and (coalesce(properties.$host, '') = ''"
            " or properties.$host in ('thelightbulb.company', 'www.thelightbulb.company'))"
        )
    return clause


_FILTER = _hogql_filter(settings.ENVIRONMENT or "")

HOGQL_FUNNEL = f"""
select toStartOfWeek(timestamp, 1) as week, event, uniq(distinct_id) as owners
from events
where event in ({_FUNNEL_EVENT_SQL}) and {_FILTER}
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by week, event order by week, event limit 500
"""

# Same event list/window as HOGQL_FUNNEL, bucketed by day for the daily chart.
HOGQL_FUNNEL_DAILY = f"""
select toDate(timestamp) as day, event, uniq(distinct_id) as owners
from events
where event in ({_FUNNEL_EVENT_SQL}) and {_FILTER}
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by day, event order by day, event limit 2000
"""

# Organic / referral share of signups (SEO plan §6). Reads the EVENT's own
# first-touch properties (stamped on every capture by the frontend's
# attributionProperties(), src/lib/analytics.ts) rather than
# person.properties.$initial_*, which is empty under identified_only
# profiles for anything captured before the person exists.
#
# `signup_started` means ONLY "Clerk account created" — CTA clicks are
# `signup_cta_clicked`. So this is genuinely signups-by-source.
HOGQL_SIGNUP_SOURCES = f"""
select coalesce(nullIf(properties.first_touch_utm_source, ''),
                nullIf(properties.first_touch_referring_domain, ''), 'direct') as source,
       uniq(distinct_id) as owners
from events
where event = 'signup_started' and {_FILTER} and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by source order by owners desc limit 50
"""


def _monday(d: date) -> date:
    """The Monday opening the ISO week `d` falls in."""
    return d - timedelta(days=d.weekday())


def week_of(ts: str | None) -> str | None:
    """Monday-start ISO date for an ISO-8601 timestamp (or None)."""
    dt = _parse_ts(ts) if ts else None
    return None if dt is None else _monday(_utc_date(dt)).isoformat()


def day_of(ts: str | None) -> str | None:
    """UTC ISO date for an ISO-8601 timestamp (or None)."""
    dt = _parse_ts(ts) if ts else None
    return None if dt is None else _utc_date(dt).isoformat()


def _weeks(now: datetime, n: int) -> list[str]:
    """The last n Monday buckets, oldest first, ending with the current (partial) week."""
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday - timedelta(weeks=i)).isoformat() for i in range(n - 1, -1, -1)]


def _days(now: datetime, n: int) -> list[str]:
    """The last n calendar days, oldest first, ending with today (UTC date of `now`)."""
    today = now.date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _parse_ts(v) -> datetime | None:
    """Parse a DynamoDB ISO string (Z / +00:00) into an aware UTC datetime, so
    comparisons never mix naive and aware values. None in -> None out; callers
    must skip a row whose timestamp is missing rather than crash the whole
    endpoint on one malformed row."""
    if v is None:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _utc_date(dt: datetime) -> date:
    """The UTC calendar date — the definitions count *UTC* days, and a stored
    offset other than Z would otherwise silently bucket by local date."""
    return dt.astimezone(timezone.utc).date()


def _rate(num: int, den: int) -> float | None:
    """A share, or None when there is nothing to divide by — never a fake 0%."""
    return (num / den) if den else None


def _normalize_messages(messages: list[dict]) -> dict[str, list[dict]]:
    """Raw ``MSG#`` rows -> ``{owner_id: [{room, kind, sender, at, dispatched}]}``,
    each list in time order. A row missing its author kind, owner, room or
    timestamp is dropped rather than failing the whole read; ``system`` authors
    are neither human nor agent and count for nothing."""
    out: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        author = m.get("author") or {}
        kind = author.get("kind")
        at = _parse_ts(m.get("createdAt"))
        owner, room = m.get("ownerId"), m.get("roomId")
        if kind not in ("human", "agent") or at is None or not owner or not room:
            continue
        out[str(owner)].append(
            {
                "room": str(room),
                "kind": kind,
                "sender": str(author.get("id") or ""),
                "at": at,
                "dispatched": bool(m.get("dispatchTargets")),
            }
        )
    for rows in out.values():
        rows.sort(key=lambda r: r["at"])
    return out


def _activated_at(owner_messages: list[dict], start: datetime) -> datetime | None:
    """The instant BOTH activation conditions hold, or None.

    Scope is the WORKSPACE, not one person: inside the 7-day window opening at
    `start`, the workspace's humans TOGETHER must have sent
    ``ACTIVATION_MIN_MESSAGES`` room messages spanning ``ACTIVATION_MIN_DAYS``
    distinct UTC days, AND an agent must have posted in one of its rooms.
    Two humans sending three messages each over two days activates the
    workspace; one human sending four does not. Returning the instant (not a
    bool) is what makes week-4 retention measurable.
    """
    end = start + timedelta(days=ACTIVATION_WINDOW_DAYS)
    window = [m for m in owner_messages if start <= m["at"] <= end]  # time-ordered
    agent_at = next((m["at"] for m in window if m["kind"] == "agent"), None)
    if agent_at is None:
        return None
    days: set[date] = set()
    human_at: datetime | None = None
    for n, m in enumerate((h for h in window if h["kind"] == "human"), start=1):
        days.add(_utc_date(m["at"]))
        if n >= ACTIVATION_MIN_MESSAGES and len(days) >= ACTIVATION_MIN_DAYS:
            human_at = m["at"]
            break
    return None if human_at is None else max(human_at, agent_at)


def _answered(messages: list[dict], since: datetime) -> dict:
    """Answered rate + p50 reply seconds over dispatched human messages since `since`.

    A message is answered when the next agent message in the SAME room lands
    within ``ANSWER_WINDOW_S``. The agent index is NOT clipped to `since`, so a
    human message at the very edge of the window is still matched to its real
    reply rather than counted as a miss.
    """
    agents_by_room: dict[str, list[datetime]] = defaultdict(list)
    for m in messages:
        if m["kind"] == "agent":
            agents_by_room[m["room"]].append(m["at"])
    for stamps in agents_by_room.values():
        stamps.sort()
    dispatched = answered = 0
    latencies: list[float] = []
    for m in messages:
        if m["kind"] != "human" or not m["dispatched"] or m["at"] < since:
            continue
        dispatched += 1
        stamps = agents_by_room.get(m["room"], [])
        i = bisect_left(stamps, m["at"])
        if i < len(stamps):
            delta = (stamps[i] - m["at"]).total_seconds()
            if delta <= ANSWER_WINDOW_S:
                answered += 1
                latencies.append(delta)
    return {
        "dispatched": dispatched,
        "answered": answered,
        "rate": _rate(answered, dispatched),
        "p50_reply_s": round(statistics.median(latencies), 1) if latencies else None,
    }


def _blank_messaging(seg: dict) -> dict:
    """The messaging scan failed: every figure it feeds renders EMPTY, never a
    green zero — that is the promise the page's own error banner makes.

    Billing-fed figures survive (workspace totals, signups, paying), because the
    DDB read that produced them succeeded. `None` is what the client turns into
    an em dash and what a chart leaves as a gap.
    """
    seg["activation"] = {"activated": None, "measurable": None, "rate": None}
    seg["weekly_active"] = [
        {"week": w["week"], "workspaces": None, "multiplayer": None, "multiplayer_share": None}
        for w in seg["weekly_active"]
    ]
    seg["answered"] = {"dispatched": None, "answered": None, "rate": None, "p50_reply_s": None}
    seg["cohorts"] = [{**c, "measurable": None, "activated": None} for c in seg["cohorts"]]
    seg["retention_w4"] = {"eligible": None, "retained": None, "rate": None}
    seg["messaging"] = {"rooms": None, "human_messages_this_week": None}
    seg["daily"] = {
        **seg["daily"],
        "human_messages": [{"day": d["day"], "n": None} for d in seg["daily"]["human_messages"]],
        "activated": [{"day": d["day"], "n": None} for d in seg["daily"]["activated"]],
    }
    return seg


def derive(
    *,
    billing: list[dict],
    messages: list[dict],
    rooms: list[dict],
    gate_blocked: dict,
    dlq_depth: int | None,
    posthog_funnel: list[list],
    posthog_sources: list[list],
    posthog_funnel_daily: list[list],
    internal: set[str],
    now: datetime,
    weeks: int = WINDOW_WEEKS,
    messaging_ok: bool = True,
) -> dict:
    by_owner = _normalize_messages(messages)
    rooms_by_owner = Counter(str(r.get("ownerId")) for r in rooms if r.get("ownerId"))
    week_list = _weeks(now, weeks)
    day_list = _days(now, WINDOW_DAYS)
    this_week = week_list[-1]
    window_start = now - timedelta(days=WINDOW_DAYS)

    def segment(rows: list[dict]) -> dict:
        owner_set = {b["owner_id"] for b in rows}
        seg_messages = [m for o in owner_set for m in by_owner.get(o, [])]

        paying = [b for b in rows if b.get("subscription_status") == PAYING_STATUS]
        trial = [b for b in rows if b.get("subscription_status") == "trial_local"]
        trial_expired = [b for b in trial if float(b.get("trial_end") or 0) < now.timestamp()]
        signups = Counter(week_of(b.get("created_at")) for b in rows)
        workspaces = {
            "total": len(rows),
            "paying": len(paying),
            "paying_by_plan": dict(Counter(b.get("plan") or "standard" for b in paying)),
            "paying_by_provider": dict(Counter(b.get("provider_choice") or "none" for b in paying)),
            "trial_active": len(trial) - len(trial_expired),
            "trial_expired": len(trial_expired),
            "signups_by_week": [{"week": w, "n": signups.get(w, 0)} for w in week_list],
        }

        # 1. Activation. A workspace counts once its 7-day window has closed OR
        # it already activated inside a window still open — otherwise yesterday's
        # signups would drag the rate down as if they had failed.
        activated_at: dict[str, datetime] = {}
        cohort_week: dict[str, str] = {}
        measurable: set[str] = set()
        for b in rows:
            oid = b["owner_id"]
            start = _parse_ts(b.get("trial_started") or b.get("created_at"))
            if start is None:
                continue
            cohort_week[oid] = _monday(_utc_date(start)).isoformat()
            at = _activated_at(by_owner.get(oid, []), start)
            if at is not None:
                activated_at[oid] = at
                measurable.add(oid)
            elif start + timedelta(days=ACTIVATION_WINDOW_DAYS) <= now:
                measurable.add(oid)
        activation = {
            "activated": len(activated_at),
            "measurable": len(measurable),
            "rate": _rate(len(activated_at), len(measurable)),
        }

        # 2. Weekly active workspaces + multiplayer share. Keyed over every week
        # in the data (not just the axis) so retention can look weeks 4 back.
        senders_by_week: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for o in owner_set:
            for m in by_owner.get(o, []):
                if m["kind"] == "human":
                    senders_by_week[_monday(_utc_date(m["at"])).isoformat()][o].add(m["sender"])
        weekly_active = []
        for w in week_list:
            active = senders_by_week.get(w, {})
            multiplayer = sum(1 for s in active.values() if len(s) >= 2)
            weekly_active.append(
                {
                    "week": w,
                    "workspaces": len(active),
                    "multiplayer": multiplayer,
                    "multiplayer_share": _rate(multiplayer, len(active)),
                }
            )

        # 3. Answered rate + p50 reply, over the same window as every other panel.
        answered = _answered(seg_messages, window_start)

        # 4. Signup-week cohorts: how many activated, how many now pay.
        # ponytail: "paying" is the CURRENT status, not a trial_local -> active
        # transition — nothing records the transition, so a converted-then-cancelled
        # workspace reads as never converted. Fine while cancellations are countable
        # by hand; needs a stored converted_at when they aren't.
        paying_set = {b["owner_id"] for b in paying}
        cohorts = []
        for w in week_list:
            members = [o for o, cw in cohort_week.items() if cw == w]
            cohorts.append(
                {
                    "week": w,
                    "signups": len(members),
                    "measurable": sum(1 for o in members if o in measurable),
                    "activated": sum(1 for o in members if o in activated_at),
                    "paying": sum(1 for o in members if o in paying_set),
                }
            )

        # 5. Week-4 retention of activated workspaces. Only cohorts whose 4th week
        # has fully passed are eligible — an in-flight week is not a miss.
        eligible = retained = 0
        for oid, at in activated_at.items():
            week4 = _monday(_utc_date(at)) + timedelta(weeks=RETENTION_WEEKS)
            if week4 + timedelta(weeks=1) > now.date():
                continue
            eligible += 1
            if oid in senders_by_week.get(week4.isoformat(), {}):
                retained += 1
        retention_w4 = {"eligible": eligible, "retained": retained, "rate": _rate(retained, eligible)}

        human_by_week = Counter(_monday(_utc_date(m["at"])).isoformat() for m in seg_messages if m["kind"] == "human")
        human_by_day = Counter(_utc_date(m["at"]).isoformat() for m in seg_messages if m["kind"] == "human")
        signups_by_day = Counter(day_of(b.get("created_at")) for b in rows)
        activated_by_day = Counter(_utc_date(at).isoformat() for at in activated_at.values())
        return {
            "workspaces": workspaces,
            "activation": activation,
            "weekly_active": weekly_active,
            "answered": answered,
            "cohorts": cohorts,
            "retention_w4": retention_w4,
            "messaging": {
                "rooms": sum(rooms_by_owner.get(o, 0) for o in owner_set),
                "human_messages_this_week": human_by_week.get(this_week, 0),
            },
            "daily": {
                "signups": [{"day": d, "n": signups_by_day.get(d, 0)} for d in day_list],
                "human_messages": [{"day": d, "n": human_by_day.get(d, 0)} for d in day_list],
                "activated": [{"day": d, "n": activated_by_day.get(d, 0)} for d in day_list],
            },
        }

    def _segment(rows: list[dict]) -> dict:
        """One segment, blanked where the messaging scan could not supply it."""
        seg = segment(rows)
        return seg if messaging_ok else _blank_messaging(seg)

    ext_rows = [b for b in billing if b["owner_id"] not in internal]
    int_rows = [b for b in billing if b["owner_id"] in internal]
    # HOGQL_FUNNEL_DAILY's query window is deliberately wider than the days axis -- the axis is what the payload promises.
    day_set = set(day_list)
    return {
        "generated_at": now.isoformat(),
        "window_weeks": weeks,
        "weeks": week_list,
        "days": day_list,
        "segments": {"external": _segment(ext_rows), "internal": _segment(int_rows)},
        "internal_owner_ids": sorted(internal),
        "gate_blocked_7d": (gate_blocked or {}).get("totals", []),
        "gate_blocked_daily": (gate_blocked or {}).get("daily", []),
        "dlq_depth": dlq_depth,
        "posthog": {
            "funnel": [{"week": str(r[0])[:10], "event": r[1], "owners": r[2]} for r in posthog_funnel],
            "signup_sources": [{"source": r[0], "owners": r[1]} for r in posthog_sources],
            "funnel_daily": [
                {"day": str(r[0])[:10], "event": r[1], "owners": r[2]}
                for r in posthog_funnel_daily
                if str(r[0])[:10] in day_set
            ],
        },
    }


_CACHE_TTL_S = 60
_cache: tuple[float, dict | None, str, list[dict]] | None = None  # (expires_at, data, state, errors)


async def _read_ddb() -> dict:
    from core.repositories import billing_repo

    return {"billing": await billing_repo.scan_all()}


async def _read_messaging() -> dict:
    from core.repositories import messaging_repo

    messages, rooms = await messaging_repo.scan_growth_rows()
    return {"messages": messages, "rooms": rooms}


async def _read_posthog() -> dict:
    from core.services.posthog_admin import hogql

    funnel, sources, funnel_daily = await asyncio.gather(
        hogql(HOGQL_FUNNEL), hogql(HOGQL_SIGNUP_SOURCES), hogql(HOGQL_FUNNEL_DAILY)
    )
    for name, res in (("funnel", funnel), ("sources", sources), ("funnel_daily", funnel_daily)):
        if res["error"]:
            raise RuntimeError(f"hogql {name}: {res['error']}")
    if any(res["stubbed"] for res in (funnel, sources, funnel_daily)):
        raise RuntimeError("hogql stubbed: POSTHOG_PROJECT_API_KEY/POSTHOG_PROJECT_ID unset")
    return {"funnel": funnel["results"], "sources": sources["results"], "funnel_daily": funnel_daily["results"]}


async def _read_cloudwatch() -> dict:
    """Both CloudWatch reads under one source key — same client, same grant, so
    they fail together and the page marks one domain, not two half-truths."""
    from core.services.ops import dlq, gate_blocked

    blocked, depth = await asyncio.gather(gate_blocked.last_7d(), dlq.work_dlq_depth())
    return {"gate_blocked": blocked, "dlq_depth": depth}


# Clerk round-trip per owner (run_analytics caches 1h/owner); cap concurrency
# so a cold compute() on a large fleet doesn't burst Clerk all at once.
_INTERNAL_CHECK_CONCURRENCY = asyncio.Semaphore(8)


async def _internal_set(owner_ids: list[str]) -> set[str]:
    from core.services.run_analytics import resolve_internal_owner

    async def _check(o: str) -> bool:
        async with _INTERNAL_CHECK_CONCURRENCY:
            return await resolve_internal_owner(o)

    flags = await asyncio.gather(*(_check(o) for o in owner_ids))
    return {o for o, f in zip(owner_ids, flags) if f}


async def compute() -> tuple[dict | None, str, list[dict]]:
    """Read every source (each fail-safe via envelope.read_source), derive, cache 60s.

    DDB is load-bearing (no billing rows → nothing to segment) → ``unavailable``.
    The internal/external split is ALSO load-bearing — every aggregate below
    assumes it ran BEFORE aggregation — so a Clerk failure there is likewise
    ``unavailable``, never a silent "everyone is external" misclassification.
    Any other source failing → ``partial`` with the domain in errors, and that
    section renders empty on the page rather than green.
    """
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    (ddb, ddb_state, ddb_err), (msg, _, msg_err), (ph, _, ph_err), (cw, _, cw_err) = await asyncio.gather(
        env.read_source("ddb", _read_ddb, timeout_s=10.0),
        env.read_source("messaging", _read_messaging, timeout_s=15.0),
        env.read_source("posthog", _read_posthog, timeout_s=20.0),
        env.read_source("cloudwatch", _read_cloudwatch, timeout_s=8.0),
    )
    if ddb is None:
        return None, env.UNAVAILABLE, ddb_err

    owner_ids = [b["owner_id"] for b in ddb["billing"]]
    internal, _, clerk_err = await env.read_source("clerk", lambda: _internal_set(owner_ids), timeout_s=10.0)
    if internal is None:
        return None, env.UNAVAILABLE, ddb_err + clerk_err

    errors = msg_err + ph_err + cw_err
    state = env.PARTIAL if errors else env.OK

    msg = msg or {"messages": [], "rooms": []}
    ph = ph or {"funnel": [], "sources": [], "funnel_daily": []}
    cw = cw or {}
    try:
        data = derive(
            **ddb,
            **msg,
            gate_blocked=cw.get("gate_blocked") or {},
            dlq_depth=cw.get("dlq_depth"),
            posthog_funnel=ph["funnel"],
            posthog_sources=ph["sources"],
            posthog_funnel_daily=ph["funnel_daily"],
            internal=internal,
            now=datetime.now(timezone.utc),
            messaging_ok=not msg_err,
        )
        data["sources"] = {
            "ddb": ddb_state,
            "messaging": "ok" if not msg_err else "unavailable",
            "posthog": "ok" if not ph_err else "unavailable",
            "cloudwatch": "ok" if not cw_err else "unavailable",
            "clerk": "ok",
        }
    except Exception as exc:  # noqa: BLE001 — fail-safe by contract, same shape as the DDB-down path
        logger.exception("growth derive failed")
        return None, env.UNAVAILABLE, errors + [{"domain": "derive", "reason": str(exc)}]
    _cache = (time.monotonic() + _CACHE_TTL_S, data, state, errors)
    return data, state, errors
