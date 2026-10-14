"""Tests for the Teams Issues BFF."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.services import trial_cap

from main import app
from routers.teams.deps import TeamsContext
from routers.teams.issues import patch_issue
from routers.teams.schemas import PatchIssueBody


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
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_issues_proxies_with_session(client, monkeypatch):
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/issues", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_list_issues_forwards_query_params(client, monkeypatch):
    """The Tasks surface drives server-side search/sort/status filtering by
    passing query params; the BFF forwards them to upstream's issues-list
    route (camelCased), and omits ``params`` entirely when none are set."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={
            "q": "deploy",
            "status": "todo,in_progress",
            "sortField": "updated",
            "sortDir": "desc",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params={
            "q": "deploy",
            "status": "todo,in_progress",
            "sortField": "updated",
            "sortDir": "desc",
        },
    )


def test_list_issues_forwards_exclude_routine_executions(client, monkeypatch):
    """The Tasks panel hides routine-spawned rows by default. Upstream reads the
    flag as the literal string "true", and it must only be sent when asked for —
    a default-on ``params`` would change every other caller's result set."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"excludeRoutineExecutions": "true"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params={"excludeRoutineExecutions": "true"},
    )


def test_list_issues_omits_exclude_routine_executions_when_false(client, monkeypatch):
    """Toggling routine tasks back on must send no filter at all, not
    ``excludeRoutineExecutions=false`` (which upstream would ignore anyway) —
    and must not manufacture an empty ``params`` dict."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"excludeRoutineExecutions": "false"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


@pytest.mark.parametrize(
    "value,status",
    [("22222222-2222-2222-2222-222222222222", 200), ("ceo", 422), ("../x", 422)],
)
def test_list_issues_participant_agent_filter(client, monkeypatch, value, status):
    """The agent Dashboard's Recent Tasks: ``participantAgentId`` forwards
    verbatim and, like ``assignee``, must be a uuid (upstream casts it)."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"participantAgentId": value},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == status
    if status == 200:
        admin.list_issues.assert_awaited_once_with(
            company_id="co_abc", session_cookie="cookie", params={"participantAgentId": value}
        )
    else:
        admin.list_issues.assert_not_awaited()


def test_list_issues_rejects_bad_sort_dir(client, monkeypatch):
    """A malformed ``sortDir`` 422s at the boundary before any upstream call."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"sortDir": "sideways"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.list_issues.assert_not_awaited()


def test_list_issues_rejects_malformed_assignee(client, monkeypatch):
    """A non-uuid ``assignee`` would make upstream throw a uuid-cast 500 that
    leaks the company path, so it 422s locally first (mirrors ``project``)."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"assignee": "not-a-uuid"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.list_issues.assert_not_awaited()


def test_list_issues_accepts_null_assignee_sentinel(client, monkeypatch):
    """``assignee="null"`` is the upstream unassigned-filter sentinel and is
    forwarded as ``assigneeAgentId=null``."""
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value={"issues": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues",
        params={"assignee": "null"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params={"assigneeAgentId": "null"},
    )


def test_list_issues_wraps_bare_array_response(client, monkeypatch):
    """Upstream's ``GET /api/companies/:co/issues`` returns a bare JSON
    array, but the frontend ``IssuesPanel`` reads ``data?.issues``.
    Without server-side wrapping the panel silently shows "No issues
    yet" even when issues exist.
    """
    admin = MagicMock()
    admin.list_issues = AsyncMock(
        return_value=[
            {"id": "iss_1", "title": "x", "status": "todo"},
            {"id": "iss_2", "title": "y", "status": "in_progress"},
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/issues", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["issues"]
    assert len(body["issues"]) == 2
    # Rich fields survive — IssuesPanel renders status / identifier / etc.
    assert body["issues"][0]["status"] == "todo"
    assert body["issues"][1]["status"] == "in_progress"


def test_create_issue_passes_whitelisted_body(client, monkeypatch):
    admin = MagicMock()
    admin.create_issue = AsyncMock(return_value={"id": "iss_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues",
        json={"title": "Bug", "priority": "high"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_issue.await_args.kwargs["body"]
    assert body == {"title": "Bug", "priority": "high"}


def test_create_issue_forwards_goal_id_as_camelcase(client, monkeypatch):
    """Tasks can roll up to a goal. ``issues.goalId`` has always existed
    upstream (createIssueSchema accepts it) — it was simply not whitelisted
    here, which is why goals could only hold sub-goals and projects."""
    admin = MagicMock()
    admin.create_issue = AsyncMock(return_value={"id": "iss_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues",
        json={"title": "Bug", "goalId": "goal_1"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_issue.await_args.kwargs["body"]
    assert body == {"title": "Bug", "goalId": "goal_1"}


def test_human_assignee_is_forwarded_and_wakes_no_agent(client, monkeypatch):
    """A task can be owned by a workspace human (Paperclip ``assigneeUserId``).
    Create and PATCH forward it as-is; the PATCH never takes the agent-only
    blocked->todo path, so no agent is handed the task."""
    admin = MagicMock()
    admin.create_issue = AsyncMock(return_value={"id": "iss_1"})
    admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    admin.get_issue = AsyncMock(return_value={"id": "iss_1", "companyId": "co_abc", "status": "blocked"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues",
        json={"title": "Call the bank", "status": "todo", "assigneeUserId": "pcu_b"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert admin.create_issue.await_args.kwargs["body"] == {
        "title": "Call the bank",
        "status": "todo",
        "assigneeUserId": "pcu_b",
    }

    r = client.patch(
        "/api/v1/teams/issues/11111111-1111-1111-1111-111111111111",
        json={"assigneeUserId": "pcu_b", "assigneeAgentId": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert admin.patch_issue.await_args.kwargs["body"] == {"assigneeUserId": "pcu_b", "assigneeAgentId": None}


def test_create_issue_rejects_unknown_field(client):
    """Unknown body fields must 422 at the FastAPI boundary
    (CreateIssueBody is extra="forbid")."""
    r = client.post(
        "/api/v1/teams/issues",
        json={"title": "Bug", "evil": "x"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_create_label_derives_color_from_name(client, monkeypatch):
    """Upstream requires a 6-digit hex colour on every label. Asking the user to
    pick one before they can categorise anything is friction for no gain, so the
    BFF derives it — deterministically, so a name always reads the same."""
    admin = MagicMock()
    admin.create_company_label = AsyncMock(return_value={"id": "lbl_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "  Marketing  "},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_company_label.await_args.kwargs["body"]
    assert body["name"] == "Marketing"
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", body["color"])

    # Deterministic: the same name must not drift colour between calls.
    admin.create_company_label.reset_mock()
    client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "Marketing"},
        headers={"Authorization": "Bearer x"},
    )
    assert admin.create_company_label.await_args.kwargs["body"]["color"] == body["color"]


def test_create_label_honours_explicit_color(client, monkeypatch):
    admin = MagicMock()
    admin.create_company_label = AsyncMock(return_value={"id": "lbl_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "Ops", "color": "#123abc"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert admin.create_company_label.await_args.kwargs["body"]["color"] == "#123abc"


def test_create_label_rejects_blank_name(client, monkeypatch):
    """``min_length`` runs against the raw string, so a whitespace-only name
    would pass validation and reach upstream as "" once the router stripped it.
    The validator strips first so the validated value is the forwarded one."""
    admin = MagicMock()
    admin.create_company_label = AsyncMock(return_value={"id": "lbl_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "   "},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.create_company_label.assert_not_awaited()


def test_create_label_rejects_bad_color(client):
    """A non-hex colour 422s locally rather than being forwarded for upstream's
    zod regex to reject opaquely."""
    r = client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "Ops", "color": "red"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_create_label_route_is_not_shadowed_by_issue_id(client, monkeypatch):
    """`/issues/labels` is a static segment registered above `/issues/{id}`.
    If that order ever flips, this POST would fall through to a parameterized
    handler and fail confusingly instead of creating a label."""
    admin = MagicMock()
    admin.create_company_label = AsyncMock(return_value={"id": "lbl_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/labels",
        json={"name": "Research"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.create_company_label.assert_awaited_once()


def test_archive_issue(client, monkeypatch):
    """`POST /teams/issues/{id}/archive` archives the issue from the inbox."""
    admin = MagicMock()
    admin.archive_issue = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/archive",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.archive_issue.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_unarchive_issue(client, monkeypatch):
    """`POST /teams/issues/{id}/unarchive` (undo) restores the issue."""
    admin = MagicMock()
    admin.unarchive_issue = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/unarchive",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.unarchive_issue.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_monitor_check_now(client, monkeypatch):
    """`POST /teams/issues/{id}/monitor/check-now` re-runs the monitor now."""
    admin = MagicMock()
    admin.monitor_check_now = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

    r = client.post(
        "/api/v1/teams/issues/iss_1/monitor/check-now",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.monitor_check_now.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_monitor_check_now_blocked_over_cap(client, monkeypatch):
    """A trial_local owner over the free-tier $3/day $3/day trial cap gets a 402
    with the structured trial_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (an immediate re-check dispatches a run right away
    server-side, so the check must run ahead of the proxy call to
    actually stop the spend)."""
    admin = MagicMock()
    admin.monitor_check_now = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))

    r = client.post(
        "/api/v1/teams/issues/iss_1/monitor/check-now",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "trial_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.monitor_check_now.assert_not_awaited()


def test_mark_issue_read(client, monkeypatch):
    admin = MagicMock()
    admin.mark_issue_read = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/mark-read",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.mark_issue_read.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_mark_issue_unread(client, monkeypatch):
    admin = MagicMock()
    admin.mark_issue_unread = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/issues/iss_1/mark-unread",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.mark_issue_unread.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_list_issue_comments(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_comments = AsyncMock(return_value={"comments": [{"id": "cmt_1", "body": "hi"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues/iss_1/comments",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"comments": [{"id": "cmt_1", "body": "hi"}]}
    admin.list_issue_comments.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


def test_list_issue_comments_enriches_human_authors(client, monkeypatch):
    """Human-authored rows get ``authorName``/``authorClerkUserId`` stamped via
    the principal→Clerk map; agent-authored rows are left untouched."""
    admin = MagicMock()
    admin.list_issue_comments = AsyncMock(
        return_value=[
            {"id": "cmt_1", "body": "hi", "authorUserId": "pcu_xyz"},
            {"id": "cmt_2", "body": "yo", "authorAgentId": "agt_1", "authorUserId": None},
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    principal_map = AsyncMock(return_value={"pcu_xyz": "u1"})
    monkeypatch.setattr(agents_mod, "build_principal_to_clerk", principal_map)
    monkeypatch.setattr(
        agents_mod,
        "_resolve_member_identity",
        AsyncMock(
            return_value={
                "userId": "u1",
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "imageUrl": None,
            }
        ),
    )

    r = client.get(
        "/api/v1/teams/issues/iss_1/comments",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["authorName"] == "Ada Lovelace"
    assert rows[0]["authorClerkUserId"] == "u1"
    assert "authorName" not in rows[1]
    assert "authorClerkUserId" not in rows[1]


def test_list_issue_comments_enriches_envelope_shape(client, monkeypatch):
    """Enrichment also reaches rows inside a ``{comments: [...]}`` envelope."""
    admin = MagicMock()
    admin.list_issue_comments = AsyncMock(
        return_value={"comments": [{"id": "cmt_1", "body": "hi", "authorUserId": "pcu_xyz"}]}
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "build_principal_to_clerk", AsyncMock(return_value={"pcu_xyz": "u1"}))
    monkeypatch.setattr(
        agents_mod,
        "_resolve_member_identity",
        AsyncMock(
            return_value={
                "userId": "u1",
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "imageUrl": None,
            }
        ),
    )

    r = client.get(
        "/api/v1/teams/issues/iss_1/comments",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["comments"][0]["authorName"] == "Ada Lovelace"


def test_list_issue_comments_enrichment_is_best_effort(client, monkeypatch):
    """A failed Clerk lookup or unmapped principal ships the raw row rather
    than failing the request."""
    admin = MagicMock()
    admin.list_issue_comments = AsyncMock(
        return_value=[
            {"id": "cmt_1", "body": "hi", "authorUserId": "pcu_xyz"},
            {"id": "cmt_2", "body": "yo", "authorUserId": "pcu_unmapped"},
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "build_principal_to_clerk", AsyncMock(return_value={"pcu_xyz": "u1"}))
    monkeypatch.setattr(
        agents_mod,
        "_resolve_member_identity",
        AsyncMock(side_effect=RuntimeError("clerk down")),
    )

    r = client.get(
        "/api/v1/teams/issues/iss_1/comments",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert all("authorName" not in row for row in rows)


def test_list_issue_comments_survives_enrichment_blowup(client, monkeypatch):
    """Enrichment must never take down the thread — an unexpected failure
    anywhere inside it returns the upstream payload verbatim."""
    admin = MagicMock()
    admin.list_issue_comments = AsyncMock(return_value=[{"id": "cmt_1", "body": "hi", "authorUserId": "pcu_xyz"}])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        agents_mod,
        "build_principal_to_clerk",
        AsyncMock(side_effect=RuntimeError("map blew up")),
    )

    r = client.get(
        "/api/v1/teams/issues/iss_1/comments",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == [{"id": "cmt_1", "body": "hi", "authorUserId": "pcu_xyz"}]


def test_add_issue_comment(client, monkeypatch):
    """POST `/teams/issues/{id}/comments` with whitelisted body."""
    admin = MagicMock()
    admin.add_issue_comment = AsyncMock(return_value={"id": "cmt_new", "body": "hello"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

    r = client.post(
        "/api/v1/teams/issues/iss_1/comments",
        json={"body": "hello"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.add_issue_comment.assert_awaited_once_with(
        issue_id="iss_1",
        body={"body": "hello"},
        session_cookie="cookie",
    )


def test_add_issue_comment_rejects_extra_fields(client):
    """Body schema is strict — extras (esp. adapterType) must 422."""
    r = client.post(
        "/api/v1/teams/issues/iss_1/comments",
        json={"body": "hello", "adapterType": "evil"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_add_issue_comment_requires_body_field(client):
    """Empty body is invalid (min_length=1)."""
    r = client.post(
        "/api/v1/teams/issues/iss_1/comments",
        json={},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_add_issue_comment_blocked_over_cap(client, monkeypatch):
    """A trial_local owner over the free-tier $3/day $3/day trial cap gets a 402
    with the structured trial_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (a comment triggers Friday to run server-side, so the
    check must run ahead of the proxy call to actually stop the spend)."""
    admin = MagicMock()
    admin.add_issue_comment = AsyncMock(return_value={"id": "cmt_new", "body": "hello"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))

    r = client.post(
        "/api/v1/teams/issues/iss_1/comments",
        json={"body": "hello"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "trial_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.add_issue_comment.assert_not_awaited()


def test_add_issue_comment_proceeds_under_cap(client, monkeypatch):
    """An under-cap (or paid) owner's comment proceeds to Paperclip as
    normal."""
    admin = MagicMock()
    admin.add_issue_comment = AsyncMock(return_value={"id": "cmt_new", "body": "hello"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

    r = client.post(
        "/api/v1/teams/issues/iss_1/comments",
        json={"body": "hello"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.add_issue_comment.assert_awaited_once_with(
        issue_id="iss_1",
        body={"body": "hello"},
        session_cookie="cookie",
    )


def test_list_labels_returns_envelope(client, monkeypatch):
    """GET /teams/issues/labels proxies upstream + wraps in {labels: [...]} envelope.

    Upstream's ``GET /api/companies/{co}/labels`` returns a bare JSON array;
    the Inbox filter chip dropdown reads ``data?.labels`` so we wrap the
    response server-side via ``envelope_list``.
    """
    admin = MagicMock()
    admin.list_company_labels = AsyncMock(
        return_value=[
            {"id": "lbl_1", "name": "bug", "color": "#dc2626"},
            {"id": "lbl_2", "name": "feature", "color": "#16a34a"},
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues/labels",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "labels" in body
    assert len(body["labels"]) == 2
    assert body["labels"][0]["name"] == "bug"
    admin.list_company_labels.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


# ---------------------------------------------------------------------------
# patch_issue — exclude_unset vs exclude_none behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_issue_forwards_explicit_null_to_unassign():
    """An admin sending {"assigneeAgentId": null} (or its snake_case Python
    equivalent) must reach Paperclip as null, not be stripped. Otherwise
    users can't unassign."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody(assignee_agent_id=None)

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    # Confirm null forwarded — was stripped under exclude_none=True before.
    fake_admin.patch_issue.assert_called_once()
    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"assigneeAgentId": None}


@pytest.mark.asyncio
async def test_patch_issue_forwards_explicit_null_goal_to_detach():
    """Detaching a task from its goal has to reach upstream as an explicit
    null. Under exclude_none it would be stripped and the link would survive
    every attempt to clear it."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody(goal_id=None)

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"goalId": None}


@pytest.mark.asyncio
async def test_patch_issue_refuses_parent_that_would_make_a_cycle():
    """Reparenting a task under its own grandchild is refused; a normal parent passes."""
    from fastapi import HTTPException

    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_admin.get_issue = AsyncMock(
        return_value={"id": "iss_grandchild", "ancestors": [{"id": "iss_child"}, {"id": "iss_1"}]}
    )
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()

    with patch("routers.teams.issues._agents", fake_agents_module):
        with pytest.raises(HTTPException) as exc:
            await patch_issue(issue_id="iss_1", body=PatchIssueBody(parent_id="iss_grandchild"), ctx=fake_ctx)
        assert exc.value.status_code == 422
        fake_admin.patch_issue.assert_not_called()

        fake_admin.get_issue.return_value = {"id": "iss_other", "ancestors": [{"id": "iss_root"}]}
        await patch_issue(issue_id="iss_1", body=PatchIssueBody(parent_id="iss_other"), ctx=fake_ctx)
        assert fake_admin.patch_issue.call_args.kwargs["body"] == {"parentId": "iss_other"}


@pytest.mark.asyncio
async def test_patch_issue_omits_unset_fields():
    """Empty PATCH body must not send unrelated keys to upstream."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody()  # nothing set

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {}


@pytest.mark.asyncio
async def test_patch_issue_forwards_camelcase_assignee_to_paperclip():
    """Paperclip's PATCH /api/issues/:id reads req.body.assigneeAgentId
    (camelCase). Our schema attribute is snake_case for Python ergonomics
    but the wire MUST be camelCase. This is the regression test for the
    'AssigneePicker click leaves issue Unassigned' bug — without this,
    Paperclip silently drops the unknown snake_case key and returns the
    unchanged issue."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    fake_admin.get_issue = AsyncMock(return_value={"id": "iss_1", "status": "todo"})
    body = PatchIssueBody(assignee_agent_id="agt_friday", project_id="proj_x")

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"assigneeAgentId": "agt_friday", "projectId": "proj_x"}


@pytest.mark.asyncio
async def test_patch_issue_forwards_blocked_by_as_camelcase_array():
    """The "Blocked by" picker sends the FULL desired blocker set. The schema
    attr is snake_case but the wire MUST be ``blockedByIssueIds`` (camelCase),
    or Paperclip silently drops the unknown key and the relation never sticks."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody(blocked_by_issue_ids=["iss_2", "iss_3"])

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"blockedByIssueIds": ["iss_2", "iss_3"]}


@pytest.mark.asyncio
async def test_patch_issue_forwards_empty_blocked_by_array_to_clear():
    """An empty array must reach Paperclip (clears all blockers), not be
    stripped — exclude_unset keeps explicitly-set keys, even ``[]``."""
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"

    body = PatchIssueBody(blocked_by_issue_ids=[])

    with patch("routers.teams.issues._agents", fake_agents_module):
        await patch_issue(issue_id="iss_1", body=body, ctx=fake_ctx)

    forwarded_body = fake_admin.patch_issue.call_args.kwargs["body"]
    assert forwarded_body == {"blockedByIssueIds": []}


def test_patch_issue_accepts_blocked_by_issue_ids_alias(client, monkeypatch):
    """The PATCH endpoint accepts the camelCase ``blockedByIssueIds`` body key
    (alias_generator=to_camel) without 422'ing under extra='forbid'."""
    admin = MagicMock()
    admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/issues/iss_1",
        json={"blockedByIssueIds": ["iss_2"]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.patch_issue.await_args.kwargs["body"]
    assert body == {"blockedByIssueIds": ["iss_2"]}


def test_delete_issue_proxies_with_session(client, monkeypatch):
    """`DELETE /teams/issues/{id}` forwards to the admin client with the
    caller's session cookie (no company_id — the route is issue-scoped)."""
    admin = MagicMock()
    admin.delete_issue = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.delete(
        "/api/v1/teams/issues/iss_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.delete_issue.assert_awaited_once_with(
        issue_id="iss_1",
        session_cookie="cookie",
    )


# ----------------------------------------------------------------------
# Artifacts gallery — view (single-document GET) + delete (document /
# work-product) proxies. These back the company Artifacts gallery's
# click-to-view + per-artifact delete. Every route is company-scoped via the
# session cookie (never a client param), and validates its path params locally
# before forwarding so a malformed key/id is rejected here rather than leaking
# the upstream company path on a cast-error 500.
# ----------------------------------------------------------------------


def test_get_document_content_proxies_with_session(client, monkeypatch):
    """`GET /teams/issues/{id}/documents/{key}` forwards the (normalized) key
    to the admin client with the caller's session cookie and returns the
    upstream document body — no company_id (issue-scoped upstream)."""
    admin = MagicMock()
    admin.get_issue_document = AsyncMock(return_value={"id": "doc_1", "key": "plan", "body": "# Plan"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues/iss_1/documents/plan",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "doc_1", "key": "plan", "body": "# Plan"}
    admin.get_issue_document.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss_1",
        key="plan",
    )


def test_get_document_content_rejects_bad_key(client, monkeypatch):
    """A malformed document key is a local 400 — never forwarded upstream."""
    admin = MagicMock()
    admin.get_issue_document = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/issues/iss_1/documents/bad!key",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400
    admin.get_issue_document.assert_not_awaited()


def test_delete_document_proxies_with_session(client, monkeypatch):
    """`DELETE /teams/issues/{id}/documents/{key}` forwards to the admin client
    with the caller's session cookie (company-scoped upstream, no company_id)."""
    admin = MagicMock()
    admin.delete_issue_document = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.delete(
        "/api/v1/teams/issues/iss_1/documents/plan",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.delete_issue_document.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss_1",
        key="plan",
    )


def test_delete_document_rejects_bad_key(client, monkeypatch):
    """A malformed document key is a local 400 — the delete never reaches
    upstream."""
    admin = MagicMock()
    admin.delete_issue_document = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.delete(
        "/api/v1/teams/issues/iss_1/documents/bad!key",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400
    admin.delete_issue_document.assert_not_awaited()


_WP = "11111111-1111-1111-1111-111111111111"


def _wp_admin(monkeypatch, rows):
    admin = MagicMock()
    admin.list_issue_work_products = AsyncMock(return_value=rows)
    admin.delete_work_product = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    return admin


def test_delete_work_product_proxies_with_session(client, monkeypatch):
    """`DELETE /teams/issues/{id}/work-products/{wpId}` forwards once the work
    product is on the (company-fenced) issue."""
    admin = _wp_admin(monkeypatch, [{"id": _WP}])
    r = client.delete(f"/api/v1/teams/issues/iss_1/work-products/{_WP}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_issue_work_products.assert_awaited_once_with(session_cookie="cookie", issue_id="iss_1")
    admin.delete_work_product.assert_awaited_once_with(session_cookie="cookie", work_product_id=_WP)


def test_delete_work_product_not_on_the_issue_is_404_and_never_forwarded(client, monkeypatch):
    """Upstream's DELETE /api/work-products/{id} is not issue-scoped."""
    admin = _wp_admin(monkeypatch, [{"id": "22222222-2222-2222-2222-222222222222"}])
    r = client.delete(f"/api/v1/teams/issues/iss_1/work-products/{_WP}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404
    admin.delete_work_product.assert_not_awaited()


def test_delete_work_product_rejects_bad_uuid(client, monkeypatch):
    """A malformed work-product id is a local 422 — never forwarded upstream."""
    admin = _wp_admin(monkeypatch, [])
    r = client.delete("/api/v1/teams/issues/iss_1/work-products/not-a-uuid", headers={"Authorization": "Bearer x"})
    assert r.status_code == 422
    admin.list_issue_work_products.assert_not_awaited()
    admin.delete_work_product.assert_not_awaited()


def test_old_unscoped_work_product_delete_is_gone(client):
    r = client.delete(f"/api/v1/teams/work-products/{_WP}", headers={"Authorization": "Bearer x"})
    assert r.status_code in (404, 405)


def _patch_fixture(current_status):
    fake_admin = MagicMock()
    fake_admin.patch_issue = AsyncMock(return_value={"id": "iss_1"})
    fake_admin.get_issue = AsyncMock(return_value={"id": "iss_1", "status": current_status})
    fake_agents_module = MagicMock()
    fake_agents_module._admin.return_value = fake_admin
    fake_ctx = MagicMock()
    fake_ctx.session_cookie = "sess=abc"
    return fake_admin, fake_agents_module, fake_ctx


@pytest.mark.asyncio
async def test_reassigning_a_blocked_issue_unblocks_it_in_the_same_patch():
    """Audit 2026-09-24 F6: a bare assignee change on a blocked issue wakes the new agent,
    whose run is cancelled as blocked, and the issue snaps back to the recovery owner."""
    fake_admin, agents, ctx = _patch_fixture("blocked")
    with patch("routers.teams.issues._agents", agents):
        await patch_issue(issue_id="iss_1", body=PatchIssueBody(assignee_agent_id="agent_forge"), ctx=ctx)
    assert fake_admin.patch_issue.call_args.kwargs["body"] == {"assigneeAgentId": "agent_forge", "status": "todo"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["todo", "in_progress", "in_review"])
async def test_reassigning_an_unblocked_issue_leaves_its_status_alone(status):
    fake_admin, agents, ctx = _patch_fixture(status)
    with patch("routers.teams.issues._agents", agents):
        await patch_issue(issue_id="iss_1", body=PatchIssueBody(assignee_agent_id="agent_forge"), ctx=ctx)
    assert fake_admin.patch_issue.call_args.kwargs["body"] == {"assigneeAgentId": "agent_forge"}


@pytest.mark.asyncio
async def test_an_explicit_status_or_an_unassign_never_reads_the_issue():
    fake_admin, agents, ctx = _patch_fixture("blocked")
    with patch("routers.teams.issues._agents", agents):
        await patch_issue(issue_id="iss_1", body=PatchIssueBody(assignee_agent_id="a", status="blocked"), ctx=ctx)
        await patch_issue(issue_id="iss_1", body=PatchIssueBody(assignee_agent_id=None), ctx=ctx)
    fake_admin.get_issue.assert_not_awaited()
    assert fake_admin.patch_issue.call_args_list[0].kwargs["body"] == {"assigneeAgentId": "a", "status": "blocked"}


def test_a_person_moving_an_agents_task_is_a_handoff(client, monkeypatch):
    """T03: a status change on an agent-assigned task emits task_moved; other edits do not."""
    admin = MagicMock()
    moved = {"id": "iss_1", "assigneeAgentId": "agent_1", "status": "done"}
    admin.patch_issue = AsyncMock(return_value=moved)
    from routers.teams import agents as agents_mod
    from routers.teams import issues as issues_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    task_moved = MagicMock()
    monkeypatch.setattr(issues_mod.handoff_analytics, "task_moved", task_moved)
    url = "/api/v1/teams/issues/11111111-1111-1111-1111-111111111111"
    assert client.patch(url, json={"title": "x"}, headers={"Authorization": "Bearer x"}).status_code == 200
    task_moved.assert_not_called()
    assert client.patch(url, json={"status": "done"}, headers={"Authorization": "Bearer x"}).status_code == 200
    task_moved.assert_called_once_with(workspace_id="o1", user_id="u1", issue=moved, status="done")
