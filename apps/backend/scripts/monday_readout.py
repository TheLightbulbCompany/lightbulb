"""Produce the founders' complete Monday product readout.

The script is intentionally a read-through job: Clerk, PostHog, Stripe and the
optional Vercel visitor endpoint are read at run time and nothing is persisted.
A scheduled production run posts the rendered page through the existing
Lightbulb room-message endpoint. Use ``--stdout`` for an operator preview.

Required environment for a production run:

* ``PLATFORM_ADMIN_USER_IDS`` (comma separated; an empty list is refused)
* ``MONDAY_READOUT_ROOM_ID`` and ``MONDAY_READOUT_BEARER_TOKEN`` unless
  ``--stdout`` is used

Credentials for Clerk, PostHog and Stripe are read from the named Secrets
Manager entries unless their corresponding environment variable is already
set. Secret values are never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

POSTHOG_PROJECT_ID = "380894"
POSTHOG_URL = f"https://us.posthog.com/api/projects/{POSTHOG_PROJECT_ID}/query/"
CLERK_URL = "https://api.clerk.com/v1"
PROD_API_URL = "https://api.thelightbulb.company/api/v1"
USER_AGENT = "lightbulb-monday-readout/1"

INTERNAL_WORKSPACE_SLUGS = (
    "lightbulb",
    "demo-lab",
    "10-06-test",
    "adenprivatetest",
    "external-review",
    "starter-roster-audit-sep-12",
    "mkt385-lightbulb-1789408375222209342",
)
CLIENT_EVENTS = ("landing_viewed", "setup_cta_clicked", "signup_cta_clicked", "signup_started")
ACQUISITION_EVENTS = (*CLIENT_EVENTS, "setup_requested")
READOUT_EVENTS = (
    "workspace_created",
    "team_formed",
    "handoff_completed",
    "session_picked_up",
    "room_message_sent",
    "setup_requested",
    "source_reported",
    *CLIENT_EVENTS,
)


def monday_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the previous complete Monday-to-Monday UTC interval."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = datetime.combine(current.date() - timedelta(days=current.weekday()), datetime.min.time(), timezone.utc)
    return end - timedelta(days=7), end


def _dt(value: str | int | float | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, timezone.utc)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _sql(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_posthog_query(
    start: datetime, end: datetime, excluded: set[str], excluded_authors: set[str]
) -> str:
    """Build the single bounded HogQL event read used by the pure summarizer."""
    earliest = start - timedelta(days=28)
    event_names = ", ".join(_sql(name) for name in READOUT_EVENTS)
    client_names = ", ".join(_sql(name) for name in CLIENT_EVENTS)
    acquisition_names = ", ".join(_sql(name) for name in ACQUISITION_EVENTS)
    exclusion = ""
    if excluded:
        exclusion = f"\n  AND toString(properties.workspace_id) NOT IN ({', '.join(_sql(v) for v in sorted(excluded))})"
    author_exclusion = ""
    if excluded_authors:
        author_exclusion = f"\n  AND toString(distinct_id) NOT IN ({', '.join(_sql(v) for v in sorted(excluded_authors))})"
    return f"""SELECT
  toString(uuid) AS event_id,
  event,
  timestamp,
  toString(properties.workspace_id) AS workspace_id,
  nullIf(toString(distinct_id), '') AS actor_id,
  nullIf(toString(distinct_id), '') AS visitor_id,
  nullIf(toString(properties.self_reported_source), '') AS self_reported_source,
  nullIf(toString(properties.first_touch_utm_source), '') AS first_touch_utm_source,
  nullIf(toString(properties.first_touch_referring_domain), '') AS first_touch_referring_domain,
  nullIf(toString(properties.last_touch_source), '') AS last_touch_source,
  nullIf(toString(properties.$host), '') AS host
FROM events
WHERE event IN ({event_names})
  AND timestamp >= toDateTime({_sql(earliest.isoformat())})
  AND timestamp < toDateTime({_sql(end.isoformat())})
  AND properties.environment = 'prod'{author_exclusion}
  AND (
    (event IN ({acquisition_names})
     AND (event NOT IN ({client_names})
          OR properties.$host IN ('thelightbulb.company', 'www.thelightbulb.company')))
    OR (event NOT IN ({client_names})
        AND notEmpty(toString(properties.workspace_id)){exclusion})
  )
ORDER BY timestamp, event_id"""


def _ids(ids: Iterable[str], organizations: dict[str, str]) -> dict[str, Any]:
    values = sorted(set(ids))
    return {
        "count": len(values),
        "workspace_ids": values,
        "workspace_names": sorted(organizations.get(value, value) for value in values),
    }


def _source(event: dict[str, Any]) -> str:
    value = event.get("first_touch_utm_source") or event.get("first_touch_referring_domain") or "direct"
    return str(value).strip().lower() or "direct"


def summarize(
    events: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    organizations: dict[str, str],
    membership_counts: dict[str, int],
    excluded: set[str],
    start: datetime,
    end: datetime,
    *,
    staff_ids: set[str] | None = None,
    vercel_visitors: int | None = None,
) -> dict[str, Any]:
    """Compute all readout metrics from fixture-shaped source records."""
    staff_ids = staff_ids or set()
    clean_events = [
        {**event, "timestamp": _dt(event["timestamp"])}
        for event in events
        if event.get("actor_id") not in staff_ids
    ]
    workspace_events = [
        event for event in clean_events if event.get("workspace_id") and event["workspace_id"] not in excluded
    ]
    acquisition_events = [
        event for event in clean_events if event["event"] in ACQUISITION_EVENTS and event.get("visitor_id")
    ]
    by_workspace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in workspace_events:
        by_workspace[event["workspace_id"]].append(event)

    setup_events = [
        event for event in workspace_events if event["event"] == "setup_requested" and start <= event["timestamp"] < end
    ]
    setup: dict[str, Any] = {"total": _ids((event["workspace_id"] for event in setup_events), organizations)}
    for key in ("self_reported_source", "last_touch_source"):
        groups: dict[str, set[str]] = defaultdict(set)
        for event in setup_events:
            groups[str(event.get(key) or "unknown").strip().lower()].add(event["workspace_id"])
        setup[key] = {source: _ids(workspaces, organizations) for source, workspaces in sorted(groups.items())}
    first_touch: dict[str, set[str]] = defaultdict(set)
    for event in setup_events:
        first_touch[_source(event)].add(event["workspace_id"])
    setup["first_touch_source"] = {
        source: _ids(workspaces, organizations) for source, workspaces in sorted(first_touch.items())
    }

    formed_ids: list[str] = []
    membership_mismatches: list[str] = []
    cohort_start, cohort_end = end - timedelta(days=9), end - timedelta(days=2)
    for workspace_id, rows in by_workspace.items():
        created = [
            row["timestamp"]
            for row in rows
            if row["event"] == "workspace_created" and cohort_start <= row["timestamp"] < cohort_end
        ]
        if not created:
            continue
        first_created = min(created)
        formed = any(
            row["event"] == "team_formed" and first_created <= row["timestamp"] <= first_created + timedelta(hours=48)
            for row in rows
        )
        if formed and membership_counts.get(workspace_id, 0) >= 2:
            formed_ids.append(workspace_id)
        elif formed:
            membership_mismatches.append(workspace_id)

    handoff_ids: list[str] = []
    cohort_start, cohort_end = end - timedelta(days=14), end - timedelta(days=7)
    for workspace_id, rows in by_workspace.items():
        created = [
            row["timestamp"]
            for row in rows
            if row["event"] == "workspace_created" and cohort_start <= row["timestamp"] < cohort_end
        ]
        if not created:
            continue
        first_created = min(created)
        if any(
            row["event"] in ("handoff_completed", "session_picked_up")
            and first_created <= row["timestamp"] <= first_created + timedelta(days=7)
            for row in rows
        ):
            handoff_ids.append(workspace_id)

    week4_ids: list[str] = []
    for workspace_id, rows in by_workspace.items():
        formed = [
            row["timestamp"]
            for row in rows
            if row["event"] == "team_formed"
            and start - timedelta(days=28) <= row["timestamp"] < end - timedelta(days=28)
        ]
        if not formed:
            continue
        first_formed = min(formed)
        if any(
            row["event"] == "room_message_sent"
            and first_formed + timedelta(days=21) <= row["timestamp"] <= first_formed + timedelta(days=28)
            for row in rows
        ):
            week4_ids.append(workspace_id)

    qualifying: dict[str, datetime] = {}
    for invoice in invoices:
        workspace_id = invoice.get("workspace_id")
        discounts = invoice.get("discounts") or invoice.get("total_discount_amounts") or []
        if (
            not workspace_id
            or workspace_id in excluded
            or int(invoice.get("amount_paid") or 0) <= 0
            or discounts
            or invoice.get("billing_reason") not in ("subscription_create", "subscription_cycle")
        ):
            continue
        paid_at = _dt(invoice["paid_at"])
        if paid_at >= end:
            continue
        qualifying[workspace_id] = min(paid_at, qualifying.get(workspace_id, paid_at))

    funnel_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for event in acquisition_events:
        if start <= event["timestamp"] < end:
            funnel_ids[_source(event)][event["event"]].add(event["visitor_id"])
    funnel = {
        source: {
            event: {"count": len(visitors), "visitor_ids": sorted(visitors)}
            for event, visitors in sorted(steps.items())
        }
        for source, steps in sorted(funnel_ids.items())
    }

    teams = _ids(formed_ids, organizations)
    teams["membership_mismatches"] = sorted(membership_mismatches)
    teams["membership_mismatch_names"] = sorted(organizations.get(value, value) for value in membership_mismatches)
    return {
        "start": start,
        "end": end,
        "setup_requests": setup,
        "teams_formed": teams,
        "first_handoff": _ids(handoff_ids, organizations),
        "week4_active": _ids(week4_ids, organizations),
        "paying_at_list": {
            "all_time": _ids(qualifying, organizations),
            "new_this_week": _ids(
                (key for key, paid_at in qualifying.items() if start <= paid_at < end), organizations
            ),
        },
        "funnel": funnel,
        "vercel_visitors": vercel_visitors,
    }


def _names(metric: dict[str, Any]) -> str:
    return ", ".join(metric.get("workspace_names", metric.get("visitor_ids", []))) or "none"


def _metric(label: str, metric: dict[str, Any]) -> str:
    return f"- {label}: **{metric['count']}** — {_names(metric)}"


def render_readout(result: dict[str, Any]) -> str:
    """Render one compact Markdown page; every count is followed by its names."""
    lines = [
        f"# Monday readout · {result['start']:%-d %b}–{(result['end'] - timedelta(days=1)):%-d %b %Y} (UTC)",
        "",
        "## Core numbers",
        _metric("Setup requests", result["setup_requests"]["total"]),
    ]
    for dimension, label in (
        ("self_reported_source", "self-reported"),
        ("first_touch_source", "first touch"),
        ("last_touch_source", "last touch"),
    ):
        for source, metric in result["setup_requests"][dimension].items():
            lines.append(f"  - {label} · {source}: **{metric['count']}** — {_names(metric)}")
    lines.extend(
        [
            _metric("Teams formed within 48h (Clerk ≥2 members)", result["teams_formed"]),
            f"  - Clerk membership mismatches: **{len(result['teams_formed']['membership_mismatches'])}** — "
            + (", ".join(result["teams_formed"]["membership_mismatch_names"]) or "none"),
            _metric("First handoff in week one", result["first_handoff"]),
            _metric("Week-4 active", result["week4_active"]),
            _metric("Paying at list · all time", result["paying_at_list"]["all_time"]),
            _metric("Paying at list · new this week", result["paying_at_list"]["new_this_week"]),
            "",
            "## Acquisition funnel by source",
        ]
    )
    ordered_sources = sorted(result["funnel"], key=lambda value: (value != "flickmeon.ai", value))
    for source in ordered_sources:
        steps = result["funnel"][source]
        lines.append(f"- **{source}**")
        base = steps.get("landing_viewed", {"count": 0})["count"]
        for event in ("landing_viewed", "setup_cta_clicked", "setup_requested", "signup_cta_clicked", "signup_started"):
            metric = steps.get(event, {"count": 0, "visitor_ids": []})
            percentage = f" ({metric['count'] / base:.0%})" if base >= 10 else ""
            lines.append(f"  - {event}: **{metric['count']}**{percentage} — {_names(metric)}")
    visitors = result["vercel_visitors"]
    lines.extend(["", f"Vercel visitors: **{visitors}**" if visitors is not None else "Vercel visitors: unavailable"])
    return "\n".join(lines)


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, body: Any = None) -> Any:
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    request = Request(url, data=encoded, headers=all_headers, method=method)
    for attempt in range(6):
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS APIs or explicit operator URL
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                detail = exc.read().decode(errors="replace")[:300]
                raise RuntimeError(f"{method} {url.split('?')[0]} failed ({exc.code}): {detail}") from exc
            time.sleep(min(float(exc.headers.get("Retry-After") or 2 ** (attempt + 1)), 60))
    raise AssertionError("unreachable")


def _secret(env_name: str, secret_id: str) -> str:
    if value := os.environ.get(env_name):
        return value
    raw = subprocess.check_output(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_id,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        text=True,
    ).strip()
    if raw.startswith("{"):
        parsed = json.loads(raw)
        for key in (env_name, env_name.lower(), "api_key", "secret_key"):
            if parsed.get(key):
                return str(parsed[key])
        raise RuntimeError(f"secret {secret_id} did not contain {env_name}")
    return raw


class Clerk:
    def __init__(self, secret: str):
        self.headers = {"Authorization": f"Bearer {secret}"}

    def get(self, path: str, **params: Any) -> Any:
        return _request("GET", f"{CLERK_URL}{path}?{urlencode(params)}", headers=self.headers)

    def organizations(self) -> list[dict[str, Any]]:
        organizations: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.get("/organizations", limit=500, offset=offset, order_by="created_at")
            rows = page.get("data", page if isinstance(page, list) else [])
            organizations.extend(rows)
            if len(rows) < 500:
                return organizations
            offset += len(rows)

    def query_organizations(self, query: str) -> list[dict[str, Any]]:
        page = self.get("/organizations", query=query, limit=100, offset=0)
        return page.get("data", page if isinstance(page, list) else [])

    def member_user_ids(self, workspace_id: str) -> set[str]:
        user_ids: set[str] = set()
        offset = 0
        while True:
            page = self.get(f"/organizations/{workspace_id}/memberships", limit=500, offset=offset)
            rows = page.get("data", [])
            for row in rows:
                public_user_data = row.get("public_user_data") or {}
                user_id = public_user_data.get("user_id") or row.get("user_id")
                if user_id:
                    user_ids.add(str(user_id))
            if len(rows) < 500:
                return user_ids
            offset += len(rows)


def resolve_organizations(clerk: Clerk, admin_ids: set[str]) -> tuple[dict[str, str], set[str]]:
    if not admin_ids:
        raise RuntimeError("PLATFORM_ADMIN_USER_IDS is empty; refusing to risk counting internal workspaces")
    organizations = clerk.organizations()
    by_id = {org["id"]: org.get("name") or org.get("slug") or org["id"] for org in organizations}
    by_slug = {org.get("slug"): org["id"] for org in organizations if org.get("slug")}
    missing = set(INTERNAL_WORKSPACE_SLUGS) - set(by_slug)
    if missing:
        raise RuntimeError(f"internal workspace slugs were not found in Clerk: {', '.join(sorted(missing))}")
    excluded = {by_slug[slug] for slug in INTERNAL_WORKSPACE_SLUGS}
    return by_id, excluded


def fetch_events(
    api_key: str,
    query: str,
    *,
    page_size: int = 50_000,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Fetch a stable, bounded PostHog event window without accepting truncation."""
    events: list[dict[str, Any]] = []
    expected_columns: list[str] | None = None
    for page_number in range(max_pages):
        offset = page_number * page_size
        payload = _request(
            "POST",
            POSTHOG_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            body={
                "query": {
                    "kind": "HogQLQuery",
                    "query": f"{query}\nLIMIT {page_size} OFFSET {offset}",
                }
            },
        )
        columns = payload.get("columns") or payload.get("results", {}).get("columns")
        rows = payload.get("results")
        if isinstance(rows, dict):
            rows = rows.get("results")
        if not columns or rows is None:
            raise RuntimeError("PostHog returned an unexpected query result")
        if expected_columns is None:
            expected_columns = columns
        elif columns != expected_columns:
            raise RuntimeError("PostHog changed columns between paginated results")
        events.extend(dict(zip(columns, row, strict=True)) for row in rows)
        if len(rows) < page_size:
            return events
    raise RuntimeError("PostHog event query reached its pagination limit; refusing an incomplete readout")


def fetch_invoices(secret: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {secret}"}
    invoices: list[dict[str, Any]] = []
    starting_after: str | None = None
    while True:
        params: list[tuple[str, Any]] = [("limit", 100), ("status", "paid"), ("expand[]", "data.customer")]
        if starting_after:
            params.append(("starting_after", starting_after))
        page = _request("GET", f"https://api.stripe.com/v1/invoices?{urlencode(params)}", headers=headers)
        for invoice in page.get("data", []):
            customer = invoice.get("customer") or {}
            metadata = customer.get("metadata", {}) if isinstance(customer, dict) else {}
            paid_at = (invoice.get("status_transitions") or {}).get("paid_at") or invoice.get("created")
            invoices.append(
                {
                    "workspace_id": metadata.get("workspace_id"),
                    "paid_at": paid_at,
                    "amount_paid": invoice.get("amount_paid"),
                    "discounts": invoice.get("discounts") or invoice.get("total_discount_amounts") or [],
                    "billing_reason": invoice.get("billing_reason"),
                }
            )
        if not page.get("has_more") or not page.get("data"):
            return invoices
        starting_after = page["data"][-1]["id"]


def fetch_vercel_visitors(start: datetime, end: datetime) -> int | None:
    """Read an operator-provided Vercel Insights endpoint, if the connection exposes one."""
    url, token = os.environ.get("VERCEL_VISITOR_URL"), os.environ.get("VERCEL_TOKEN")
    if not url or not token:
        return None
    separator = "&" if "?" in url else "?"
    payload = _request(
        "GET",
        f"{url}{separator}{urlencode({'from': int(start.timestamp() * 1000), 'to': int(end.timestamp() * 1000)})}",
        headers={"Authorization": f"Bearer {token}"},
    )
    value = payload.get("visitors") if isinstance(payload, dict) else None
    return int(value) if value is not None else None


def post_readout(text: str) -> None:
    room_id = os.environ.get("MONDAY_READOUT_ROOM_ID")
    token = os.environ.get("MONDAY_READOUT_BEARER_TOKEN")
    if not room_id or not token:
        raise RuntimeError("MONDAY_READOUT_ROOM_ID and MONDAY_READOUT_BEARER_TOKEN are required to post")
    base = os.environ.get("LIGHTBULB_API_BASE", PROD_API_URL).rstrip("/")
    _request(
        "POST",
        f"{base}/messaging/rooms/{room_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        body={
            "text": text,
            "mentions": [],
            "client_message_id": str(uuid4()),
            "reply_to_message_id": None,
            "thread_root_message_id": None,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="preview instead of posting to the founders' room")
    parser.add_argument("--now", help="override current time for a reproducible operator preview")
    args = parser.parse_args(argv)
    now = _dt(args.now) if args.now else None
    start, end = monday_window(now)

    clerk = Clerk(_secret("CLERK_SECRET_KEY", "lightbulb/prod/clerk_secret_key"))
    admins = {value.strip() for value in os.environ.get("PLATFORM_ADMIN_USER_IDS", "").split(",") if value.strip()}
    organizations, excluded = resolve_organizations(clerk, admins)
    events = fetch_events(
        _secret("POSTHOG_PROJECT_API_KEY", "lightbulb/prod/posthog_project_api_key"),
        build_posthog_query(start, end, excluded, admins),
    )
    relevant = {event["workspace_id"] for event in events if event.get("workspace_id")}
    membership_counts = {
        workspace_id: len(clerk.member_user_ids(workspace_id) - admins)
        for workspace_id in relevant
    }
    invoices = fetch_invoices(_secret("STRIPE_SECRET_KEY", "lightbulb/prod/stripe_secret_key"))
    result = summarize(
        events,
        invoices,
        organizations,
        membership_counts,
        excluded,
        start,
        end,
        staff_ids=admins,
        vercel_visitors=fetch_vercel_visitors(start, end),
    )
    text = render_readout(result)
    if args.stdout:
        print(text)
    else:
        post_readout(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
