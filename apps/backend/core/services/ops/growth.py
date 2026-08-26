"""Growth / GTM metrics for the admin console — computed on read.

Definitions are DECIDED in docs/growth/2026-08-17-blitzscaling-strategy.md §3;
data-source research is docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md §0.1.

Sources: DynamoDB scans (owners, plans, readiness, connected providers),
the Paperclip Aurora DB via the Data API (routines + runs — the ledger for
autonomous-run attribution), PostHog HogQL (client funnel and a lossy
cross-check — never a headline), CloudWatch (gate-blocked codes).

Everything is segmented internal/external BEFORE aggregation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE
from core.services.ops import envelope as env

logger = logging.getLogger(__name__)

WINDOW_WEEKS = 8
HEADLINE_BAR = 5  # "five external owners with armed automations firing for four consecutive weeks"
HEADLINE_WEEKS = 4
_PAID = frozenset({"active", "trialing"})  # = provision_gate.is_paid_subscription

# --- SQL (Paperclip fork schema at the pinned tag; see paperclip_sql docstring) ---
SQL_COMPANIES = "SELECT id, name, created_at FROM companies LIMIT 5000"

SQL_ROUTINES = """
SELECT r.company_id, r.id, r.title, r.status,
       (r.auto_paused_at IS NOT NULL) AS auto_paused,
       r.created_at, r.updated_at,
       EXISTS (SELECT 1 FROM routine_triggers t WHERE t.routine_id = r.id AND t.enabled) AS has_enabled_trigger,
       (SELECT string_agg(DISTINCT t.kind, ',') FROM routine_triggers t WHERE t.routine_id = r.id AND t.enabled) AS trigger_kinds
FROM routines r
LIMIT 5000
"""

# title is a code constant with no quote characters; keep it that way (it is
# interpolated into SQL literals below).
# ponytail: seq-scans heartbeat_runs (every fork index is company_id-leading;
# no created_at index) on the tenant-facing 0-4 ACU Aurora, once per 60s while
# an admin has the page open — fine at tens of tenants; when it isn't, add a
# created_at index in the fork or drive SQL_FIRST_AUTONOMOUS from routine_runs.
# heartbeat_runs has no routine_id: attribution is issue -> dispatching routine_run -> source.
# ponytail: LEFT JOIN routine_runs on linked_issue_id can double-count an issue that
# two routine_runs link (coalesced runs carry no linked issue today); revisit if seen.
SQL_RUNS_BY_WEEK = f"""
SELECT hr.company_id,
       to_char(date_trunc('week', hr.created_at), 'YYYY-MM-DD') AS week,
       CASE WHEN r.title = '{NIGHTLY_CEO_ROUTINE_TITLE}' THEN 'nightly_ceo'
            WHEN rr.source IN ('schedule', 'webhook') THEN 'autonomous'
            ELSE 'human' END AS kind,
       count(*) FILTER (WHERE hr.status = 'succeeded') AS ok,
       count(*) AS total
FROM heartbeat_runs hr
LEFT JOIN issues i ON i.id::text = hr.context_snapshot->>'issueId'
LEFT JOIN routine_runs rr ON rr.linked_issue_id = i.id
LEFT JOIN routines r ON r.id = rr.routine_id
WHERE hr.created_at > now() - interval '{WINDOW_WEEKS + 1} weeks'
GROUP BY 1, 2, 3
"""

# ponytail: seq-scans heartbeat_runs (every fork index is company_id-leading;
# no created_at index) on the tenant-facing 0-4 ACU Aurora, once per 60s while
# an admin has the page open — fine at tens of tenants; when it isn't, add a
# created_at index in the fork or drive SQL_FIRST_AUTONOMOUS from routine_runs.
SQL_FIRST_AUTONOMOUS = f"""
SELECT hr.company_id, min(hr.created_at) AS first_ok
FROM heartbeat_runs hr
JOIN issues i ON i.id::text = hr.context_snapshot->>'issueId'
JOIN routine_runs rr ON rr.linked_issue_id = i.id
JOIN routines r ON r.id = rr.routine_id
WHERE hr.status = 'succeeded' AND rr.source IN ('schedule', 'webhook')
  AND r.title <> '{NIGHTLY_CEO_ROUTINE_TITLE}'
GROUP BY 1
"""

# --- HogQL (cross-check only). toStartOfWeek(., 1) = Monday, matching date_trunc('week'). ---
HOGQL_RUNS = f"""
select toStartOfWeek(timestamp, 1) as week, properties.initiation_type as kind, count() as n
from events
where event = 'agent_run_completed' and properties.outcome = 'success'
  and toString(properties.is_internal) != 'true'
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by week, kind order by week, kind limit 500
"""

HOGQL_FUNNEL = f"""
select toStartOfWeek(timestamp, 1) as week, event, uniq(distinct_id) as owners
from events
where event in ('landing_viewed', 'signup_started', 'trial_started', 'first_run_brief_submitted',
                'trial_first_agent_response', 'chat_completed', 'trial_converted',
                'subscription_activated', 'subscription_cancelled', 'automation_armed')
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by week, event order by week, event limit 500
"""

# Organic / referral share of signups (SEO plan §6). person_profiles is
# identified_only and signup_started fires pre-identify, so
# person.properties.$initial_* is empty here — read the EVENT's own
# first-touch properties instead (stamped on every capture by the frontend's
# attributionProperties(), src/lib/analytics.ts).
HOGQL_SIGNUP_SOURCES = f"""
select coalesce(nullIf(properties.first_touch_utm_source, ''),
                nullIf(properties.first_touch_referring_domain, ''), 'direct') as source,
       uniq(distinct_id) as owners
from events
where event = 'signup_started' and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by source order by owners desc limit 50
"""


def week_of(ts: str | None) -> str | None:
    """Monday-start ISO date for an ISO-8601 timestamp (or None)."""
    if not ts:
        return None
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _weeks(now: datetime, n: int) -> list[str]:
    """The last n Monday buckets, oldest first, ending with the current (partial) week."""
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday - timedelta(weeks=i)).isoformat() for i in range(n - 1, -1, -1)]


def _iso(v) -> str | None:
    return None if v is None else str(v)


def _parse_ts(v) -> datetime | None:
    """Parse a DynamoDB ISO string (Z / +00:00) or a Data API 'YYYY-MM-DD HH:MM:SS[.ffffff]'
    (space-separated, no zone) into an aware UTC datetime, so comparisons against `cutoff`
    never mix naive and aware values. None in -> None out; callers must skip a row whose
    timestamp is missing rather than crash the whole endpoint on one malformed row."""
    if v is None:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def derive(
    *,
    billing: list[dict],
    containers: list[dict],
    paperclip: list[dict],
    integrations: list[dict],
    companies: list[dict],
    routines: list[dict],
    runs: list[dict],
    first_autonomous: list[dict],
    gate_blocked: list[dict],
    posthog_runs: list[list],
    posthog_funnel: list[list],
    posthog_sources: list[list],
    internal: set[str],
    now: datetime,
    weeks: int = WINDOW_WEEKS,
) -> dict:
    # company <-> owner. Org members share a company; prefer the seeded (owner) row.
    company_owner: dict[str, str] = {}
    for row in paperclip:
        uid = str(row.get("user_id") or "")
        cid = row.get("company_id")
        if uid.startswith("__") or not cid:
            continue
        owner = row.get("org_id") or uid
        if cid not in company_owner or row.get("seeded_agent_ids"):
            company_owner[cid] = owner
    owner_company = {o: c for c, o in company_owner.items()}
    company_name = {c["id"]: c.get("name") for c in companies}

    def name_of(owner_id: str) -> str:
        return company_name.get(owner_company.get(owner_id, ""), None) or owner_id

    container_by_owner = {c["owner_id"]: c for c in containers}
    # Friday's kickoff outcome lives on the OWNER's paperclip-companies row.
    # Org member rows also carry seeded_agent_ids but never bootstrap_state,
    # so only a row that actually HAS bootstrap_state may set/override it —
    # a member row can never clobber the owner row's real value.
    bootstrap_by_owner: dict[str, str] = {}
    for row in paperclip:
        uid = str(row.get("user_id") or "")
        if uid.startswith("__"):
            continue
        owner = row.get("org_id") or uid
        state = row.get("bootstrap_state")
        if state is not None:
            bootstrap_by_owner[owner] = state
        elif owner not in bootstrap_by_owner:
            bootstrap_by_owner[owner] = "not_started"
    routines_by_company: dict[str, list[dict]] = defaultdict(list)
    for r in routines:
        routines_by_company[r["company_id"]].append(r)
    first_ok_by_company = {r["company_id"]: _iso(r.get("first_ok")) for r in first_autonomous}
    providers_by_owner: dict[str, list[str]] = defaultdict(list)
    for it in integrations:
        if it.get("status", "connected") == "connected":
            providers_by_owner[it["owner_id"]].append(it["provider"])
    week_list = _weeks(now, weeks)
    headline_weeks = week_list[-HEADLINE_WEEKS - 1 : -1]  # last 4 COMPLETED weeks

    def is_armed(r: dict) -> bool:
        return r["status"] == "active" and bool(r["has_enabled_trigger"]) and r["title"] != NIGHTLY_CEO_ROUTINE_TITLE

    def segment(rows: list[dict]) -> dict:
        owner_ids = [b["owner_id"] for b in rows]
        owner_set = set(owner_ids)
        company_set = {owner_company[o] for o in owner_ids if o in owner_company}

        paid = [b for b in rows if b.get("subscription_status") in _PAID]
        trial = [b for b in rows if b.get("subscription_status") == "trial_local"]
        trial_expired = [b for b in trial if float(b.get("trial_end") or 0) < now.timestamp()]
        signups = Counter(week_of(b.get("created_at")) for b in rows)
        owners = {
            "total": len(rows),
            "paid": len(paid),
            "paid_by_plan": dict(Counter(b.get("plan") or "standard" for b in paid)),
            "paid_by_provider": dict(Counter(b.get("provider_choice") or "none" for b in paid)),
            "trial_active": len(trial) - len(trial_expired),
            "trial_expired": len(trial_expired),
            "signups_by_week": [{"week": w, "n": signups.get(w, 0)} for w in week_list],
        }

        # Activation gate — furthest stage reached, in order.
        stage_names = [
            "signed_up",
            "subscribed",
            "container_ready",
            "friday_first_run_delivered",
            "automation_armed",
            "first_autonomous_success",
        ]
        flag_names = stage_names[1:]  # every stage but "signed_up", which is always true
        owner_rows, stage_counts, flag_counts = [], Counter(), Counter()
        for b in rows:
            oid = b["owner_id"]
            cid = owner_company.get(oid, "")
            flags = [
                True,
                b.get("subscription_status") in _PAID,
                container_by_owner.get(oid, {}).get("last_gateway_handshake_at") is not None,
                bootstrap_by_owner.get(oid) == "delivered",
                any(is_armed(r) for r in routines_by_company.get(cid, [])),
                first_ok_by_company.get(cid) is not None,
            ]
            reached = 0
            for i, f in enumerate(flags):
                if not f:
                    break
                reached = i
            for s in stage_names[: reached + 1]:
                stage_counts[s] += 1
            # Order-independent per-flag counts (F8): the DECIDED ladder above
            # stops at the first false flag (e.g. a failed Friday kickoff), so
            # a LATER flag — like automation armed — can be true without ever
            # showing in stage_counts. Both must be visible so the two panels
            # can never silently contradict each other.
            for name, f in zip(flag_names, flags[1:]):
                if f:
                    flag_counts[name] += 1
            owner_rows.append(
                {
                    "owner_id": oid,
                    "name": name_of(oid),
                    "stage": stage_names[reached],
                    "plan": b.get("plan"),
                    "status": b.get("subscription_status"),
                    "bootstrap_state": bootstrap_by_owner.get(oid, "not_started"),
                    "first_autonomous_at": first_ok_by_company.get(cid),
                }
            )
        owner_rows.sort(key=lambda o: (-stage_names.index(o["stage"]), o["name"]))
        activation = {
            "stages": {s: stage_counts.get(s, 0) for s in stage_names},
            "flags": {s: flag_counts.get(s, 0) for s in flag_names},
            "owners": owner_rows,
        }

        # North Star — per week, external successful runs by kind; nightly CEO reported, never counted.
        seg_runs = [r for r in runs if r["company_id"] in company_set]
        active_by_week: dict[str, set[str]] = defaultdict(set)
        sums: dict[str, Counter] = defaultdict(Counter)
        for r in seg_runs:
            ok = int(r.get("ok") or 0)
            sums[r["week"]][r["kind"]] += ok
            if r["kind"] == "autonomous" and ok > 0:
                active_by_week[r["week"]].add(company_owner[r["company_id"]])
        ns_weeks = []
        for w in week_list:
            a, h = sums[w]["autonomous"], sums[w]["human"]
            ns_weeks.append(
                {
                    "week": w,
                    "owners_active": len(active_by_week[w]),
                    "autonomous_ok": a,
                    "human_ok": h,
                    "nightly_ok": sums[w]["nightly_ceo"],
                    "autonomous_share": (a / (a + h)) if (a + h) else None,
                }
            )
        consecutive = sorted(name_of(o) for o in owner_set if all(o in active_by_week[w] for w in headline_weeks))
        headline = {"bar": HEADLINE_BAR, "count": len(consecutive), "owners": consecutive, "weeks": headline_weeks}

        # Retention — weekly active owners is ns_weeks.owners_active; survival over routines ≥30d old.
        cutoff = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        cutoff = cutoff - timedelta(days=30)
        survival = Counter()
        for cid in company_set:
            for r in routines_by_company.get(cid, []):
                if r["title"] == NIGHTLY_CEO_ROUTINE_TITLE:
                    continue
                created_at = _parse_ts(r["created_at"])
                if created_at is None or created_at > cutoff:
                    continue
                if r["status"] == "archived":
                    survival["archived"] += 1
                elif r["status"] == "paused":
                    survival["auto_paused" if r["auto_paused"] else "paused_manual"] += 1
                elif is_armed(r):
                    survival["armed"] += 1
        retention = {
            "weekly_active_owners": [{"week": w["week"], "n": w["owners_active"]} for w in ns_weeks],
            "automation_survival": {
                k: survival.get(k, 0) for k in ("armed", "paused_manual", "auto_paused", "archived")
            },
        }

        # Depth
        armed_per_owner = []
        for oid in sorted(owner_set, key=name_of):
            armed = [r for r in routines_by_company.get(owner_company.get(oid, ""), []) if is_armed(r)]
            if armed:
                kinds = sorted({k for r in armed for k in (r.get("trigger_kinds") or "").split(",") if k})
                armed_per_owner.append(
                    {"owner_id": oid, "name": name_of(oid), "armed": len(armed), "trigger_kinds": kinds}
                )
        providers_per_owner = [
            {"owner_id": oid, "name": name_of(oid), "providers": sorted(providers_by_owner[oid])}
            for oid in sorted(owner_set, key=name_of)
            if providers_by_owner.get(oid)
        ]
        depth = {"armed_per_owner": armed_per_owner, "providers_per_owner": providers_per_owner}

        # Counter-metrics
        auto_paused, archived_7d = [], []
        for cid in sorted(company_set, key=lambda c: name_of(company_owner[c])):
            oid = company_owner[cid]
            for r in routines_by_company.get(cid, []):
                if r["status"] == "paused" and r["auto_paused"]:
                    auto_paused.append(
                        {"owner_id": oid, "name": name_of(oid), "title": r["title"], "since": _iso(r.get("updated_at"))}
                    )
                if r["status"] == "archived":
                    created = _parse_ts(r["created_at"])
                    updated = _parse_ts(r["updated_at"])
                    if created is not None and updated is not None and updated - created <= timedelta(days=7):
                        archived_7d.append({"owner_id": oid, "name": name_of(oid), "title": r["title"]})
        counters = {"auto_paused": auto_paused, "archived_within_7d": archived_7d}

        # "never_armed" is the bug class found 2026-08-26 (paid owners the arm
        # never reached) — it must stay visible, not fold into "opted out".
        nightly = {
            "armed": sum(1 for b in paid if b.get("nightly_ceo_armed_company") and not b.get("nightly_ceo_opted_out")),
            "opted_out": sum(1 for b in paid if b.get("nightly_ceo_opted_out")),
            "never_armed": sum(
                1 for b in paid if not b.get("nightly_ceo_armed_company") and not b.get("nightly_ceo_opted_out")
            ),
        }
        return {
            "owners": owners,
            "activation": activation,
            "north_star": {"weeks": ns_weeks},
            "headline": headline,
            "retention": retention,
            "depth": depth,
            "counters": counters,
            "nightly_ceo": nightly,
        }

    ext_rows = [b for b in billing if b["owner_id"] not in internal]
    int_rows = [b for b in billing if b["owner_id"] in internal]
    return {
        "generated_at": now.isoformat(),
        "window_weeks": weeks,
        "weeks": week_list,
        "segments": {"external": segment(ext_rows), "internal": segment(int_rows)},
        "internal_owner_ids": sorted(internal),
        "gate_blocked_7d": gate_blocked,
        "posthog": {
            "runs": [{"week": str(r[0])[:10], "kind": r[1], "n": r[2]} for r in posthog_runs],
            "funnel": [{"week": str(r[0])[:10], "event": r[1], "owners": r[2]} for r in posthog_funnel],
            "signup_sources": [{"source": r[0], "owners": r[1]} for r in posthog_sources],
        },
    }


_CACHE_TTL_S = 60
_cache: tuple[float, dict | None, str, list[dict]] | None = None  # (expires_at, data, state, errors)


async def _read_ddb() -> dict:
    from core.repositories import billing_repo, container_repo, integrations_repo
    from core.repositories.paperclip_repo import PaperclipRepo

    billing, containers, paperclip, integrations = await asyncio.gather(
        billing_repo.scan_all(),
        container_repo.scan_all(),
        PaperclipRepo(table_name="paperclip-companies").scan_all_items(),
        integrations_repo.scan_all(),
    )
    return {"billing": billing, "containers": containers, "paperclip": paperclip, "integrations": integrations}


async def _read_aurora() -> dict:
    from core.services.paperclip_sql import select_rows

    companies, routines, runs, first = await asyncio.gather(
        select_rows(SQL_COMPANIES),
        select_rows(SQL_ROUTINES),
        select_rows(SQL_RUNS_BY_WEEK),
        select_rows(SQL_FIRST_AUTONOMOUS),
    )
    return {"companies": companies, "routines": routines, "runs": runs, "first_autonomous": first}


async def _read_posthog() -> dict:
    from core.services.posthog_admin import hogql

    runs, funnel, sources = await asyncio.gather(hogql(HOGQL_RUNS), hogql(HOGQL_FUNNEL), hogql(HOGQL_SIGNUP_SOURCES))
    for name, res in (("runs", runs), ("funnel", funnel), ("sources", sources)):
        if res["error"]:
            raise RuntimeError(f"hogql {name}: {res['error']}")
    if any(res["stubbed"] for res in (runs, funnel, sources)):
        raise RuntimeError("hogql stubbed: POSTHOG_PROJECT_API_KEY/POSTHOG_PROJECT_ID unset")
    return {"runs": runs["results"], "funnel": funnel["results"], "sources": sources["results"]}


async def _read_gate_blocked() -> list[dict]:
    from core.services.ops import gate_blocked

    return await gate_blocked.last_7d()


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

    DDB is load-bearing (no owners → nothing to segment) → ``unavailable``.
    The internal/external split is ALSO load-bearing — every aggregate below
    assumes it ran BEFORE aggregation — so a Clerk failure there is likewise
    ``unavailable``, never a silent "everyone is external" misclassification.
    Any other source failing → ``partial`` with the domain in errors, and that
    section renders empty on the page rather than green.
    """
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    (ddb, ddb_state, ddb_err), (aurora, _, aurora_err), (ph, _, ph_err), (gate, _, gate_err) = await asyncio.gather(
        env.read_source("ddb", _read_ddb, timeout_s=10.0),
        env.read_source("aurora", _read_aurora, timeout_s=15.0),
        env.read_source("posthog", _read_posthog, timeout_s=20.0),
        env.read_source("cloudwatch", _read_gate_blocked, timeout_s=8.0),
    )
    if ddb is None:
        return None, env.UNAVAILABLE, ddb_err

    owner_ids = [b["owner_id"] for b in ddb["billing"]]
    internal, _, clerk_err = await env.read_source("clerk", lambda: _internal_set(owner_ids), timeout_s=10.0)
    if internal is None:
        return None, env.UNAVAILABLE, ddb_err + clerk_err

    errors = aurora_err + ph_err + gate_err
    state = env.PARTIAL if errors else env.OK

    aurora = aurora or {"companies": [], "routines": [], "runs": [], "first_autonomous": []}
    ph = ph or {"runs": [], "funnel": [], "sources": []}
    try:
        data = derive(
            **ddb,
            **aurora,
            gate_blocked=gate or [],
            posthog_runs=ph["runs"],
            posthog_funnel=ph["funnel"],
            posthog_sources=ph["sources"],
            internal=internal,
            now=datetime.now(timezone.utc),
        )
        data["sources"] = {
            "ddb": ddb_state,
            "aurora": "ok" if not aurora_err else "unavailable",
            "posthog": "ok" if not ph_err else "unavailable",
            "cloudwatch": "ok" if not gate_err else "unavailable",
            "clerk": "ok",
        }
    except Exception as exc:  # noqa: BLE001 — fail-safe by contract, same shape as the DDB-down path
        logger.exception("growth derive failed")
        return None, env.UNAVAILABLE, errors + [{"domain": "derive", "reason": str(exc)}]
    _cache = (time.monotonic() + _CACHE_TTL_S, data, state, errors)
    return data, state, errors
