"""Tests for the Teams Issues BFF (Task 9)."""

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


def test_create_issue_rejects_unknown_field(client):
    """Unknown body fields must 422 at the FastAPI boundary
    (CreateIssueBody is extra="forbid")."""
    r = client.post(
        "/api/v1/teams/issues",
        json={"title": "Bug", "evil": "x"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


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


def test_add_issue_comment(client, monkeypatch):
    """POST `/teams/issues/{id}/comments` with whitelisted body."""
    admin = MagicMock()
    admin.add_issue_comment = AsyncMock(return_value={"id": "cmt_new", "body": "hello"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

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
