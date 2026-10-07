# apps/backend/tests/unit/routers/teams/test_issue_document_annotations.py
"""Tests for the plan-document review BFF routes (A3).

Covers upsert / revisions / restore / annotation-thread routes on
``routers/teams/issues.py``: forwarding shape, list envelopes, the boundary
doc-key + thread-id 400 guards, the free-tier cap gate on the spend front
doors (create thread / reply), and the ``_Strict`` forbid-extras schemas.
"""

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
        paperclip_user_id="pcu",
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


def _install_admin(monkeypatch, admin):
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    """Default: the free-tier cap is clear. Individual tests override to assert gating."""
    import routers.teams.issues as issues_mod

    monkeypatch.setattr(issues_mod, "require_trial_runway", AsyncMock(return_value=None))


_VALID_SELECTOR = {
    "quote": {"exact": "the plan body", "prefix": "before ", "suffix": " after"},
    "position": {
        "normalizedStart": 10,
        "normalizedEnd": 23,
        "markdownStart": 10,
        "markdownEnd": 23,
    },
}

_H = {"Authorization": "Bearer x"}


# --------------------------------------------------------------------------
# Upsert (board edit → new revision)
# --------------------------------------------------------------------------


def test_upsert_document_forwards_and_preserves_status(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(201, {"id": "d1", "key": "plan"}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/plan",
        headers=_H,
        json={"format": "markdown", "body": "# Plan\nhello", "changeSummary": "init"},
    )
    assert r.status_code == 201
    assert r.json() == {"id": "d1", "key": "plan"}
    admin.upsert_issue_document.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        key="plan",
        body={"format": "markdown", "body": "# Plan\nhello", "changeSummary": "init"},
    )


def test_upsert_document_update_returns_200(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(200, {"id": "d1"}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/plan",
        headers=_H,
        json={"format": "markdown", "body": "updated"},
    )
    assert r.status_code == 200


def test_upsert_document_normalizes_key(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(200, {"id": "d1"}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/PLAN",
        headers=_H,
        json={"format": "markdown", "body": "x"},
    )
    assert r.status_code == 200
    assert admin.upsert_issue_document.await_args.kwargs["key"] == "plan"


def test_upsert_document_rejects_bad_key(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(200, {}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/bad!key",
        headers=_H,
        json={"format": "markdown", "body": "x"},
    )
    assert r.status_code == 400
    admin.upsert_issue_document.assert_not_awaited()


def test_upsert_document_rejects_bad_format(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(200, {}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/plan",
        headers=_H,
        json={"format": "html", "body": "x"},
    )
    assert r.status_code == 422
    admin.upsert_issue_document.assert_not_awaited()


def test_upsert_document_forbids_extra_field(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_document = AsyncMock(return_value=(200, {}))
    _install_admin(monkeypatch, admin)

    r = client.put(
        "/api/v1/teams/issues/iss1/documents/plan",
        headers=_H,
        json={"format": "markdown", "body": "x", "lockedByAgentId": "sneaky"},
    )
    assert r.status_code == 422
    admin.upsert_issue_document.assert_not_awaited()


# --------------------------------------------------------------------------
# Revisions + restore
# --------------------------------------------------------------------------


def test_list_revisions_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_document_revisions = AsyncMock(
        return_value=[{"id": "r1", "revisionNumber": 1}, {"id": "r2", "revisionNumber": 2}]
    )
    _install_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/issues/iss1/documents/plan/revisions", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"revisions": [{"id": "r1", "revisionNumber": 1}, {"id": "r2", "revisionNumber": 2}]}
    admin.list_issue_document_revisions.assert_awaited_once_with(session_cookie="cookie", issue_id="iss1", key="plan")


def test_restore_revision_forwards(client, monkeypatch):
    admin = MagicMock()
    admin.restore_issue_document_revision = AsyncMock(return_value={"id": "d1", "latestRevisionNumber": 3})
    _install_admin(monkeypatch, admin)

    rid = "11111111-2222-3333-4444-555555555555"
    r = client.post(
        f"/api/v1/teams/issues/iss1/documents/plan/revisions/{rid}/restore",
        headers=_H,
    )
    assert r.status_code == 200
    assert r.json() == {"id": "d1", "latestRevisionNumber": 3}
    admin.restore_issue_document_revision.assert_awaited_once_with(
        session_cookie="cookie", issue_id="iss1", key="plan", revision_id=rid
    )


def test_restore_revision_rejects_non_uuid(client, monkeypatch):
    admin = MagicMock()
    admin.restore_issue_document_revision = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/documents/plan/revisions/not-a-uuid/restore",
        headers=_H,
    )
    assert r.status_code == 400
    admin.restore_issue_document_revision.assert_not_awaited()


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------


def test_list_annotations_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_document_annotations = AsyncMock(
        return_value=[{"id": "t1", "status": "open"}, {"id": "t2", "status": "resolved"}]
    )
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/issues/iss1/documents/plan/annotations?status=all&includeComments=true",
        headers=_H,
    )
    assert r.status_code == 200
    assert r.json() == {"threads": [{"id": "t1", "status": "open"}, {"id": "t2", "status": "resolved"}]}
    admin.list_document_annotations.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        key="plan",
        status="all",
        include_comments=True,
    )


def test_list_annotations_rejects_bad_status(client, monkeypatch):
    admin = MagicMock()
    admin.list_document_annotations = AsyncMock(return_value=[])
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/issues/iss1/documents/plan/annotations?status=bogus",
        headers=_H,
    )
    assert r.status_code == 422
    admin.list_document_annotations.assert_not_awaited()


def test_create_annotation_forwards_camelcase_and_gates_cap(client, monkeypatch):
    admin = MagicMock()
    admin.create_document_annotation = AsyncMock(return_value={"id": "t_new"})
    _install_admin(monkeypatch, admin)
    import routers.teams.issues as issues_mod

    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(issues_mod, "require_trial_runway", gate)

    r = client.post(
        "/api/v1/teams/issues/iss1/documents/plan/annotations",
        headers=_H,
        json={
            "baseRevisionId": "11111111-2222-3333-4444-555555555555",
            "baseRevisionNumber": 2,
            "selector": _VALID_SELECTOR,
            "body": "please clarify this",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"id": "t_new"}
    gate.assert_awaited_once_with("o1")
    forwarded = admin.create_document_annotation.await_args.kwargs["body"]
    assert forwarded["baseRevisionId"] == "11111111-2222-3333-4444-555555555555"
    assert forwarded["baseRevisionNumber"] == 2
    assert forwarded["selector"]["quote"]["exact"] == "the plan body"
    assert forwarded["selector"]["position"]["normalizedStart"] == 10


def test_create_annotation_blocked_when_over_cap(client, monkeypatch):
    from fastapi import HTTPException

    admin = MagicMock()
    admin.create_document_annotation = AsyncMock(return_value={"id": "t_new"})
    _install_admin(monkeypatch, admin)
    import routers.teams.issues as issues_mod

    async def _blocked(_owner):
        raise HTTPException(status_code=402, detail="daily limit reached")

    monkeypatch.setattr(issues_mod, "require_trial_runway", _blocked)

    r = client.post(
        "/api/v1/teams/issues/iss1/documents/plan/annotations",
        headers=_H,
        json={
            "baseRevisionId": "11111111-2222-3333-4444-555555555555",
            "baseRevisionNumber": 2,
            "selector": _VALID_SELECTOR,
            "body": "please clarify this",
        },
    )
    assert r.status_code == 402
    admin.create_document_annotation.assert_not_awaited()


def test_create_annotation_forbids_extra_field(client, monkeypatch):
    admin = MagicMock()
    admin.create_document_annotation = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/documents/plan/annotations",
        headers=_H,
        json={
            "baseRevisionId": "11111111-2222-3333-4444-555555555555",
            "baseRevisionNumber": 2,
            "selector": _VALID_SELECTOR,
            "body": "x",
            "authorAgentId": "smuggled",
        },
    )
    assert r.status_code == 422
    admin.create_document_annotation.assert_not_awaited()


def test_add_annotation_comment_forwards_and_gates(client, monkeypatch):
    admin = MagicMock()
    admin.add_document_annotation_comment = AsyncMock(return_value={"id": "c_new"})
    _install_admin(monkeypatch, admin)
    import routers.teams.issues as issues_mod

    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(issues_mod, "require_trial_runway", gate)

    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.post(
        f"/api/v1/teams/issues/iss1/documents/plan/annotations/{tid}/comments",
        headers=_H,
        json={"body": "a reply"},
    )
    assert r.status_code == 200
    gate.assert_awaited_once_with("o1")
    admin.add_document_annotation_comment.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        key="plan",
        thread_id=tid,
        body={"body": "a reply"},
    )


def test_add_annotation_comment_rejects_bad_thread_id(client, monkeypatch):
    admin = MagicMock()
    admin.add_document_annotation_comment = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/documents/plan/annotations/nope/comments",
        headers=_H,
        json={"body": "a reply"},
    )
    assert r.status_code == 400
    admin.add_document_annotation_comment.assert_not_awaited()


def test_update_annotation_thread_resolves(client, monkeypatch):
    admin = MagicMock()
    admin.update_document_annotation_thread = AsyncMock(return_value={"id": "t1", "status": "resolved"})
    _install_admin(monkeypatch, admin)

    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.patch(
        f"/api/v1/teams/issues/iss1/documents/plan/annotations/{tid}",
        headers=_H,
        json={"status": "resolved"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "t1", "status": "resolved"}
    admin.update_document_annotation_thread.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        key="plan",
        thread_id=tid,
        body={"status": "resolved"},
    )


def test_update_annotation_thread_rejects_bad_status(client, monkeypatch):
    admin = MagicMock()
    admin.update_document_annotation_thread = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.patch(
        f"/api/v1/teams/issues/iss1/documents/plan/annotations/{tid}",
        headers=_H,
        json={"status": "archived"},
    )
    assert r.status_code == 422
    admin.update_document_annotation_thread.assert_not_awaited()
