"""Teams BFF — Inbox.

Read-mostly resource. Reuses the shared ``_ctx`` Depends helper and
shared ``_admin()`` singleton from ``routers.teams.agents`` so we
don't duplicate the auth chain or leak a fresh httpx client per
request. ``_admin`` is referenced via the imported module (rather
than imported by name) so unit tests can monkeypatch
``agents._admin`` once and have every Teams router pick it up.

Exposes a full Inbox surface: filter-aware listing + heartbeat-run +
live-run sub-routes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list

router = APIRouter()
_ctx = _agents._ctx

# Paperclip ``projects.id`` is a Postgres ``uuid`` column, so a malformed
# ``project`` filter makes upstream throw a uuid-cast error → 500 (whose body
# leaked the internal company path). Validate the shape here so a bad value is a
# local 422 before any upstream call. The user-id filters (assignee/creator) map
# to ``text`` columns upstream, so they don't have this cast-error failure mode.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


# Tab → upstream filter composition. Mirrors what Paperclip's own Inbox.tsx
# does for board users (GET /api/companies/{co}/issues with these filters).
#
# Status set must match upstream's INBOX_MINE_ISSUE_STATUSES at
# paperclip/packages/shared/src/constants.ts (backlog,todo,in_progress,
# in_review,blocked,done). The previous string contained `pending`/`review`
# which aren't members of upstream's ISSUE_STATUSES enum, and omitted
# `backlog` — the default status for newly-created issues — so freshly
# created items never showed up in the creator's Mine tab.
_INBOX_MINE_STATUSES = "backlog,todo,in_progress,in_review,blocked,done"

_TAB_FILTERS: dict[str, dict[str, str]] = {
    "mine": {
        "touchedByUserId": "me",
        "inboxArchivedByUserId": "me",
        "status": _INBOX_MINE_STATUSES,
    },
    "recent": {
        "status": _INBOX_MINE_STATUSES,
    },
    "all": {},
    "unread": {
        "inboxArchivedByUserId": "me",
        "status": _INBOX_MINE_STATUSES,
    },
}
# Tabs that don't fetch from issues at all — they live at sibling routes.
# Frontend should hit /teams/inbox/runs, /teams/approvals, etc. directly,
# but if a stale call lands here we return an empty envelope.
_TABS_NOT_ON_ISSUES = frozenset({"approvals", "runs", "joins"})


@router.get("/inbox")
async def list_inbox(
    ctx: TeamsContext = Depends(_ctx),
    tab: Optional[str] = Query(default=None, pattern=r"^(mine|recent|all|unread|approvals|runs|joins)$"),
    status: Optional[str] = Query(default=None, max_length=40),
    project: Optional[str] = Query(default=None, pattern=_UUID_RE),
    assignee: Optional[str] = Query(default=None, max_length=80),
    creator: Optional[str] = Query(default=None, max_length=80),
    search: Optional[str] = Query(default=None, max_length=200),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    """List inbox items for the signed-in board user.

    Calls upstream ``GET /api/companies/{companyId}/issues`` with
    tab-derived filter params. Mirrors what Paperclip's own Inbox.tsx
    does for board users — the prior implementation called
    ``/api/agents/me/inbox-lite`` which is AGENT-ONLY and 401's our
    board-user session cookie.

    Tabs ``approvals`` / ``runs`` / ``joins`` route through sibling
    endpoints (``/teams/approvals``, ``/teams/inbox/runs``,
    ``/teams/inbox/live-runs``); for those tab values this handler
    returns an empty envelope so the frontend can keep one tab handler.

    When the caller passes no params at all (bare ``/teams/inbox``),
    default to the ``mine`` filter set. The previous ``inbox-lite``
    upstream returned the caller's personal queue by default; mirroring
    that here keeps existing naive callers (the current InboxPanel,
    which fetches ``/inbox`` with no params) from flooding with
    company-wide issues. If any filter is set explicitly, treat it as
    an advanced query and don't overlay the mine-filter.
    """
    if tab in _TABS_NOT_ON_ISSUES:
        return {"items": []}

    no_caller_filters = (
        tab is None
        and status is None
        and project is None
        and assignee is None
        and creator is None
        and search is None
        and limit is None
    )
    effective_tab = "mine" if no_caller_filters else tab
    upstream_params: dict[str, str] = dict(_TAB_FILTERS.get(effective_tab or "", {}))

    # Per-filter overrides. ``status`` from the caller wins over the
    # tab-derived status default — frontend passes status explicitly only
    # when the user picks one in the filters popover.
    if status is not None:
        upstream_params["status"] = status
    if project is not None:
        upstream_params["projectId"] = project
    if assignee is not None:
        upstream_params["assigneeUserId"] = assignee
    if creator is not None:
        upstream_params["createdByUserId"] = creator
    if search is not None:
        upstream_params["search"] = search
    if limit is not None:
        upstream_params["limit"] = str(limit)

    kwargs: dict[str, Any] = {
        "company_id": ctx.company_id,
        "session_cookie": ctx.session_cookie,
    }
    if upstream_params:
        kwargs["params"] = upstream_params

    rows = await _agents._admin().list_issues(**kwargs)

    # Forward rich rows verbatim — InboxPage needs status / identifier /
    # priority / lastActivityAt / unread etc. to render IssueRow without
    # crashing in StatusIcon (`status.replace(...)` on undefined).
    return envelope_list(rows, "items")


# A failed run older than this needs no attention regardless of recovery —
# it's history, not an alert. Insurance for agents that never ran again
# (e.g. every routine paused), whose failures would otherwise pin the
# "Needs attention" band forever (dismissals are per-browser localStorage).
_FAILED_RUN_ATTENTION_WINDOW = timedelta(days=14)


def _parse_run_ts(value: Any) -> Optional[datetime]:
    """Parse an upstream run timestamp; None when absent/unparseable.

    Pre-Date-fix Paperclip images serialize timestamps as ``{}`` — treat
    anything non-string as unparseable rather than crashing the filter.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_ts(run: dict) -> Optional[datetime]:
    """When the run reached its terminal state (fall back to creation).

    Ordering recovery by creation time is wrong for overlapping runs: a
    failure created before a success but finishing after it is the agent's
    LATEST terminal state and must not be hidden by that success.
    """
    return (
        _parse_run_ts(run.get("finishedAt"))
        or _parse_run_ts(run.get("createdAt"))
        or _parse_run_ts(run.get("startedAt"))
    )


@router.get("/inbox/runs")
async def list_inbox_runs(ctx: TeamsContext = Depends(_ctx)):
    """List failed heartbeat runs that still NEED ATTENTION.

    Upstream's company heartbeat-runs list has no status filter (the
    ``status`` param it used to be sent was ignored), so the full recent
    list arrives either way — use it: a failure only needs attention while
    the agent has NOT completed a successful run since (once it has, the
    failure self-resolved), and never past a hard age window. Without
    this, failures linger forever: dismissals live in per-browser
    localStorage, so every browser re-surfaces the same week-old incident.

    Fails open — a run whose timestamps are unparseable (pre-Date-fix
    images send ``{}``) is kept, since hiding a real failure is worse than
    showing a stale one.
    """
    upstream = await _agents._admin().list_company_heartbeat_runs(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    rows = envelope_list(upstream, "runs")["runs"]

    last_success: dict[str, datetime] = {}
    for run in rows:
        agent_id = run.get("agentId")
        ts = _run_ts(run)
        if run.get("status") == "succeeded" and isinstance(agent_id, str) and ts:
            if agent_id not in last_success or ts > last_success[agent_id]:
                last_success[agent_id] = ts

    cutoff = datetime.now(timezone.utc) - _FAILED_RUN_ATTENTION_WINDOW

    def needs_attention(run: dict) -> bool:
        if run.get("status") != "failed":
            return False
        ts = _run_ts(run)
        if ts is None:
            return True  # fail open: unparseable timestamps keep the row
        if ts < cutoff:
            return False
        recovered_at = last_success.get(run.get("agentId") or "")
        return recovered_at is None or ts > recovered_at

    return {"runs": [run for run in rows if needs_attention(run)]}


@router.get("/inbox/live-runs")
async def list_inbox_live_runs(ctx: TeamsContext = Depends(_ctx)):
    """List currently-running heartbeat runs for the 'Live' badge.

    Forwards ``minCount=4`` so a short live-run set is padded with recent
    runs, mirroring upstream's ``MIN_DASHBOARD_RUNS`` — otherwise the
    dashboard Active-Agents strip under-populates.
    """
    return await _agents._admin().list_company_live_runs(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        min_count=4,
    )
