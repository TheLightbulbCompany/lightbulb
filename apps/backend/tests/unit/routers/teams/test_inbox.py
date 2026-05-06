"""Tests for the Teams Inbox BFF (Task 7)."""

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


def test_list_inbox_forwards_rich_issue_rows(client, monkeypatch):
    """The BFF forwards Paperclip's full Issue rows verbatim inside
    ``{items: [...]}``.

    Regression guard: PR #3c's InboxPage / IssueRow / StatusIcon read
    ``issue.status``, ``issue.identifier``, ``issue.priority``,
    ``issue.lastActivityAt`` etc. directly. The previous handler reshaped
    rows into a 4-field stub (``{id, type, title, createdAt}``) which made
    StatusIcon throw ``Cannot read properties of undefined (reading
    'replace')`` because ``issue.status`` was missing.
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


def test_dismiss_proxies_with_session(client, monkeypatch):
    admin = MagicMock()
    admin.dismiss_inbox_item = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/inbox/itm_1/dismiss",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.dismiss_inbox_item.assert_awaited_once_with(
        item_id="itm_1",
        session_cookie="cookie",
    )


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
            "project": "proj_1",
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
        "projectId": "proj_1",
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


def test_list_inbox_runs_returns_failed_runs(client, monkeypatch):
    """`GET /teams/inbox/runs` lists failed heartbeat runs (the ones the
    Inbox 'Runs' tab needs)."""
    admin = MagicMock()
    admin.list_company_heartbeat_runs = AsyncMock(return_value={"runs": [{"id": "run_1", "status": "failed"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/inbox/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"runs": [{"id": "run_1", "status": "failed"}]}
    admin.list_company_heartbeat_runs.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        status="failed",
    )


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
    admin.list_company_live_runs.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
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
