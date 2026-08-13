"""Tests for the Teams Inbox BFF."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie",
    )


@pytest.fixture
def client(teams_ctx):
    """Bypass the auth/session chain by overriding the shared ``_ctx`` dep."""
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_inbox_proxies_to_company_issues(client, monkeypatch):
    """The BFF must call ``GET /api/companies/{co}/issues`` (the board-
    user-friendly endpoint) and reshape into ``{items: []}``. A bare
    ``/teams/inbox`` (no params) defaults to the ``mine`` filter set —
    matching the personal-inbox semantic the previous ``inbox-lite``
    upstream returned by default."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/inbox", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"items": []}
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params={
            "touchedByUserId": "me",
            "inboxArchivedByUserId": "me",
            "status": "backlog,todo,in_progress,in_review,blocked,done",
        },
    )


def test_inbox_rejects_non_uuid_project_locally(client, monkeypatch):
    """``projects.id`` is a Postgres uuid upstream, so a malformed ``project``
    filter would 500 and leak the internal company path. The BFF must reject it
    as a local 422 and never call upstream."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/inbox?project=not-a-uuid", headers={"Authorization": "Bearer x"})
    assert r.status_code == 422
    admin.list_issues.assert_not_called()


def test_inbox_accepts_valid_uuid_project(client, monkeypatch):
    """A canonical UUID ``project`` is forwarded upstream as ``projectId``."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    uid = "11111111-2222-3333-4444-555555555555"
    r = client.get(f"/api/v1/teams/inbox?project={uid}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    params = admin.list_issues.await_args.kwargs["params"]
    assert params["projectId"] == uid


def test_list_inbox_forwards_rich_issue_rows(client, monkeypatch):
    """The BFF forwards Paperclip's full Issue rows verbatim inside
    ``{items: [...]}``.

    Regression guard: InboxPage / IssueRow / StatusIcon read
    ``issue.status``, ``issue.identifier``, ``issue.priority``,
    ``issue.lastActivityAt`` etc. directly. A 4-field stub reshape
    (``{id, type, title, createdAt}``) would make StatusIcon throw
    ``Cannot read properties of undefined (reading 'replace')`` because
    ``issue.status`` would be missing.
    """
    admin = MagicMock()
    admin.list_issues = AsyncMock(
        return_value=[
            {
                "id": "iss_1",
                "identifier": "PAP-1",
                "title": "Fix the inbox",
                "status": "todo",
                "priority": "high",
                "updatedAt": "2026-05-02T00:00:00Z",
            },
            {
                "id": "iss_2",
                "identifier": "PAP-2",
                "title": "Ship it",
                "status": "in_progress",
                "updatedAt": "2026-05-02T01:00:00Z",
            },
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/inbox", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["items"]
    assert len(body["items"]) == 2
    # Status / identifier / priority MUST survive the BFF — frontend renders
    # them directly.
    assert body["items"][0]["status"] == "todo"
    assert body["items"][0]["identifier"] == "PAP-1"
    assert body["items"][0]["priority"] == "high"
    assert body["items"][1]["status"] == "in_progress"


def test_list_inbox_translates_tab_mine_to_upstream_filters(client, monkeypatch):
    """tab=mine derives the upstream filter composition that Paperclip's
    own Inbox.tsx uses: touchedByUserId=me + inboxArchivedByUserId=me +
    status=INBOX_MINE_ISSUE_STATUSES (paperclip/packages/shared/src/
    constants.ts)."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/inbox?tab=mine",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    call = admin.list_issues.call_args
    assert call.kwargs["company_id"] == "co_abc"
    assert call.kwargs["session_cookie"] == "cookie"
    assert call.kwargs["params"] == {
        "touchedByUserId": "me",
        "inboxArchivedByUserId": "me",
        "status": "backlog,todo,in_progress,in_review,blocked,done",
    }


@pytest.mark.parametrize("tab", ["mine", "recent", "unread"])
def test_inbox_status_filter_matches_upstream_constants(client, monkeypatch, tab):
    """Status filter for issue-bearing tabs MUST match upstream's
    INBOX_MINE_ISSUE_STATUSES (paperclip/packages/shared/src/constants.ts).

    Regression guard: the prior string contained ``pending`` and ``review``
    which aren't members of upstream's ``ISSUE_STATUSES`` enum, and omitted
    ``backlog`` — the default status for newly-created issues — so a
    user's freshly-created issue never appeared in their own Mine tab.
    """
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        f"/api/v1/teams/inbox?tab={tab}",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    statuses = admin.list_issues.call_args.kwargs["params"]["status"].split(",")
    assert set(statuses) == {
        "backlog",
        "todo",
        "in_progress",
        "in_review",
        "blocked",
        "done",
    }


def test_list_inbox_user_status_overrides_tab_default(client, monkeypatch):
    """When the user picks an explicit status in the filters popover,
    that wins over the tab-derived status."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/inbox?tab=mine&status=blocked",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    call = admin.list_issues.call_args
    assert call.kwargs["params"]["status"] == "blocked"
    # Other tab-derived filters preserved:
    assert call.kwargs["params"]["touchedByUserId"] == "me"


def test_list_inbox_forwards_explicit_filter_params(client, monkeypatch):
    """Explicit filter params are passed through with their upstream
    field-name translation (project → projectId, assignee →
    assigneeUserId, creator → createdByUserId)."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/inbox",
        params={
            # project is a uuid upstream; assignee/creator are text user ids.
            "project": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "assignee": "user_2",
            "creator": "user_3",
            "search": "fix bug",
            "limit": 250,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    call = admin.list_issues.call_args
    assert call.kwargs["params"] == {
        "projectId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "assigneeUserId": "user_2",
        "createdByUserId": "user_3",
        "search": "fix bug",
        "limit": "250",
    }


def test_list_inbox_explicit_tab_all_clears_default_mine(client, monkeypatch):
    """When the caller explicitly picks ``tab=all``, the upstream call
    must NOT carry the mine-filter set — ``all`` is the explicit
    "show everything" tab and overrides the bare-call default."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/inbox?tab=all",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    call = admin.list_issues.call_args
    # tab=all has no filter overlays, and since the caller set ``tab``
    # explicitly the bare-call default no longer applies — so no params
    # kwarg is forwarded at all.
    assert "params" not in call.kwargs


@pytest.mark.parametrize("tab", ["approvals", "runs", "joins"])
def test_list_inbox_returns_empty_for_sibling_route_tabs(client, monkeypatch, tab):
    """approvals/runs/joins tabs route through sibling endpoints
    (/teams/approvals, /teams/inbox/runs, /teams/inbox/live-runs).
    /teams/inbox returns an empty envelope so a stale call from the
    frontend doesn't break — the frontend's tab handler stays uniform."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        f"/api/v1/teams/inbox?tab={tab}",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"items": []}
    admin.list_issues.assert_not_called()


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _mock_inbox_runs(monkeypatch, rows):
    admin = MagicMock()
    admin.list_company_heartbeat_runs = AsyncMock(return_value=rows)
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    return admin


def test_list_inbox_runs_needs_attention_semantics(client, monkeypatch):
    """`GET /teams/inbox/runs` only surfaces failures that still need
    attention: a failure the agent has since recovered from (a later
    successful run) is resolved, and a failure past the age window is
    history. Dismissals are per-browser localStorage, so without this
    every browser re-surfaces the same stale incident forever."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = [
        # Recovered: failed, then succeeded later — drop.
        {"id": "r_old_fail", "agentId": "ag_1", "status": "failed", "createdAt": _iso(now - timedelta(days=2))},
        {"id": "r_recovery", "agentId": "ag_1", "status": "succeeded", "createdAt": _iso(now - timedelta(days=1))},
        # Failure AFTER the agent's last success — keep.
        {"id": "r_regressed", "agentId": "ag_2", "status": "failed", "createdAt": _iso(now - timedelta(hours=2))},
        {"id": "r_earlier_ok", "agentId": "ag_2", "status": "succeeded", "createdAt": _iso(now - timedelta(days=1))},
        # Never recovered but ancient — aged out, drop.
        {"id": "r_ancient", "agentId": "ag_3", "status": "failed", "createdAt": _iso(now - timedelta(days=15))},
        # Never recovered, recent — keep.
        {"id": "r_fresh", "agentId": "ag_4", "status": "failed", "createdAt": _iso(now - timedelta(hours=1))},
        # Non-failed rows never surface.
        {"id": "r_running", "agentId": "ag_5", "status": "running", "createdAt": _iso(now)},
    ]
    admin = _mock_inbox_runs(monkeypatch, {"runs": rows})

    r = client.get("/api/v1/teams/inbox/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["runs"]]
    assert ids == ["r_regressed", "r_fresh"]
    admin.list_company_heartbeat_runs.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_list_inbox_runs_orders_recovery_by_terminal_time(client, monkeypatch):
    """Overlapping runs: a failure CREATED before a success but FINISHING
    after it is the agent's latest terminal state — it must stay visible.
    Recovery ordering uses finishedAt, not createdAt."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = [
        # Failure created first but finished LAST — keep.
        {
            "id": "r_slow_fail",
            "agentId": "ag_1",
            "status": "failed",
            "createdAt": _iso(now - timedelta(hours=3)),
            "finishedAt": _iso(now - timedelta(hours=1)),
        },
        # Success created later but finished BEFORE the failure did.
        {
            "id": "r_quick_ok",
            "agentId": "ag_1",
            "status": "succeeded",
            "createdAt": _iso(now - timedelta(hours=2, minutes=30)),
            "finishedAt": _iso(now - timedelta(hours=2)),
        },
    ]
    _mock_inbox_runs(monkeypatch, {"runs": rows})

    r = client.get("/api/v1/teams/inbox/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert [row["id"] for row in r.json()["runs"]] == ["r_slow_fail"]


def test_list_inbox_runs_fails_open_on_unparseable_timestamps(client, monkeypatch):
    """Pre-Date-fix Paperclip images serialize timestamps as ``{}`` — a
    failure whose age we can't establish must stay visible (hiding a real
    failure is worse than showing a stale one)."""
    _mock_inbox_runs(
        monkeypatch,
        {"runs": [{"id": "run_1", "agentId": "ag_1", "status": "failed", "createdAt": {}}]},
    )

    r = client.get("/api/v1/teams/inbox/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert [row["id"] for row in r.json()["runs"]] == ["run_1"]


def test_list_inbox_live_runs(client, monkeypatch):
    """`GET /teams/inbox/live-runs` returns currently-running runs for the
    'Live' badge on Inbox rows."""
    admin = MagicMock()
    admin.list_company_live_runs = AsyncMock(return_value={"runs": [{"id": "run_live"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/inbox/live-runs",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    # Forwards minCount=4 (upstream's MIN_DASHBOARD_RUNS) so the Active-Agents
    # strip is padded with recent runs rather than under-populated.
    admin.list_company_live_runs.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        min_count=4,
    )


def test_list_inbox_rejects_unknown_tab(client):
    """Sanity check: the regex still rejects nonsense to keep the
    forwarded query string within the documented vocabulary."""
    r = client.get(
        "/api/v1/teams/inbox",
        params={"tab": "evil-tab"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
