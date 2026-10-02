# apps/backend/tests/unit/routers/teams/test_cases.py
"""Teams BFF — Cases. Route logic with the fence stubbed (conftest), plus the
fence itself (``real_fences``): a case from another company is a 404 and nothing
is forwarded. Mirrors the fixture style of ``test_feedback.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services.paperclip_admin_client import PaperclipApiError
from main import app
from routers.teams.deps import TeamsContext

_AUTH = {"Authorization": "Bearer x"}
_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client():
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return TeamsContext(
            user_id="u1",
            org_id="o1",
            owner_id="o1",
            company_id="co_abc",
            paperclip_user_id="pcu",
            session_cookie="cookie",
        )

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def _mock_admin(monkeypatch, admin):
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)


def test_list_cases_forwards_filters_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_cases = AsyncMock(return_value=[{"id": "c1", "identifier": "LB-C1"}])
    _mock_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/cases?types=blog_post&types=tweet&status=active&includeNoProject=true&q=launch&limit=200",
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json() == {"cases": [{"id": "c1", "identifier": "LB-C1"}]}
    admin.list_cases.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        params={
            "types": ["blog_post", "tweet"],
            "status": "active",
            "includeNoProject": "true",
            "q": "launch",
            "limit": 200,
        },
    )


def test_list_cases_disabled_flag_is_a_403(client, monkeypatch):
    admin = MagicMock()
    admin.list_cases = AsyncMock(
        side_effect=PaperclipApiError(
            "GET /api/companies/co_abc/cases -> 403", status_code=403, body='{"error":"Cases are disabled"}'
        )
    )
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/cases", headers=_AUTH)
    assert r.status_code == 403
    assert r.json()["detail"] == "Cases are disabled"


def test_get_case_returns_the_fenced_case(client):
    r = client.get("/api/v1/teams/cases/LB-C7", headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == {"id": "LB-C7", "companyId": "co_abc"}


def test_patch_case_forwards_only_what_was_sent(client, monkeypatch):
    admin = MagicMock()
    admin.patch_case = AsyncMock(return_value={"id": "c1", "status": "approved"})
    _mock_admin(monkeypatch, admin)

    r = client.patch("/api/v1/teams/cases/c1", json={"status": "approved"}, headers=_AUTH)
    assert r.status_code == 200
    admin.patch_case.assert_awaited_once_with(case_id="c1", body={"status": "approved"}, session_cookie="cookie")

    admin.patch_case.reset_mock()
    r = client.patch("/api/v1/teams/cases/c1", json={"labelIds": [_UUID]}, headers=_AUTH)
    assert r.status_code == 200
    admin.patch_case.assert_awaited_once_with(case_id="c1", body={"labelIds": [_UUID]}, session_cookie="cookie")


@pytest.mark.parametrize("body", [{"status": "shipped"}, {"title": "agent-authored"}, {"fields": {}}])
def test_patch_case_rejects_what_the_board_may_not_write(client, monkeypatch, body):
    admin = MagicMock()
    admin.patch_case = AsyncMock()
    _mock_admin(monkeypatch, admin)

    r = client.patch("/api/v1/teams/cases/c1", json=body, headers=_AUTH)
    assert r.status_code == 422
    admin.patch_case.assert_not_awaited()


def test_events_and_issue_cases_envelope(client, monkeypatch):
    admin = MagicMock()
    admin.list_case_events = AsyncMock(return_value=[{"id": "e1", "kind": "created"}])
    admin.list_issue_cases = AsyncMock(return_value=[{"id": "l1", "role": "work"}])
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/cases/c1/events?limit=50", headers=_AUTH)
    assert r.json() == {"events": [{"id": "e1", "kind": "created"}]}
    admin.list_case_events.assert_awaited_once_with(case_id="c1", limit=50, session_cookie="cookie")

    r = client.get("/api/v1/teams/issues/iss1/cases", headers=_AUTH)
    assert r.json() == {"cases": [{"id": "l1", "role": "work"}]}
    admin.list_issue_cases.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")


def test_document_revisions_unwrap_the_upstream_object(client, monkeypatch):
    admin = MagicMock()
    admin.list_case_document_revisions = AsyncMock(
        return_value={"key": "Draft.v2", "document": {"id": "d1"}, "revisions": [{"id": "r1", "revisionNumber": 1}]}
    )
    admin.restore_case_document_revision = AsyncMock(return_value={"document": {"id": "d1"}})
    _mock_admin(monkeypatch, admin)

    # Case document keys are case-sensitive and may carry `.` / `:`.
    r = client.get("/api/v1/teams/cases/c1/documents/Draft.v2/revisions", headers=_AUTH)
    assert r.json() == {"revisions": [{"id": "r1", "revisionNumber": 1}]}
    admin.list_case_document_revisions.assert_awaited_once_with(case_id="c1", key="Draft.v2", session_cookie="cookie")

    r = client.post(f"/api/v1/teams/cases/c1/documents/Draft.v2/revisions/{_UUID}/restore", headers=_AUTH)
    assert r.status_code == 200
    admin.restore_case_document_revision.assert_awaited_once_with(
        case_id="c1", key="Draft.v2", revision_id=_UUID, session_cookie="cookie"
    )

    assert (
        client.post("/api/v1/teams/cases/c1/documents/Draft.v2/revisions/nope/restore", headers=_AUTH).status_code
        == 400
    )
    # A key that could rewrite the upstream path never leaves the BFF.
    assert client.get("/api/v1/teams/cases/c1/documents/.hidden/revisions", headers=_AUTH).status_code == 422


def test_document_annotations_forward(client, monkeypatch):
    admin = MagicMock()
    admin.list_case_document_annotations = AsyncMock(return_value=[{"id": "t1"}])
    admin.update_case_document_annotation_thread = AsyncMock(return_value={"id": "t1", "status": "resolved"})
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/cases/c1/documents/draft/annotations?status=all&includeComments=true", headers=_AUTH)
    assert r.json() == {"threads": [{"id": "t1"}]}
    admin.list_case_document_annotations.assert_awaited_once_with(
        case_id="c1", key="draft", status="all", include_comments=True, session_cookie="cookie"
    )

    r = client.patch(
        f"/api/v1/teams/cases/c1/documents/draft/annotations/{_UUID}", json={"status": "resolved"}, headers=_AUTH
    )
    assert r.status_code == 200
    admin.update_case_document_annotation_thread.assert_awaited_once_with(
        case_id="c1", key="draft", thread_id=_UUID, body={"status": "resolved"}, session_cookie="cookie"
    )


# ---- the fence, live ----


class _Upstream:
    headers = {"content-type": "image/png", "content-length": "3"}

    async def aiter_bytes(self):
        yield b"png"

    async def aclose(self):
        pass


@pytest.mark.real_fences
def test_foreign_case_is_404_and_never_forwarded(client, monkeypatch):
    admin = MagicMock()
    admin.get_case = AsyncMock(return_value={"id": "c1", "companyId": "co_other"})
    _mock_admin(monkeypatch, admin)

    assert client.get("/api/v1/teams/cases/c1", headers=_AUTH).status_code == 404
    assert client.patch("/api/v1/teams/cases/c1", json={"status": "done"}, headers=_AUTH).status_code == 404
    assert {name for name, *_ in admin.mock_calls} == {"get_case"}


@pytest.mark.real_fences
def test_own_case_by_identifier_forwards_its_uuid(client, monkeypatch):
    admin = MagicMock()
    admin.get_case = AsyncMock(return_value={"id": "case-uuid", "identifier": "LB-C7", "companyId": "co_abc"})
    admin.patch_case = AsyncMock(return_value={"id": "case-uuid", "status": "done"})
    _mock_admin(monkeypatch, admin)

    r = client.patch("/api/v1/teams/cases/LB-C7", json={"status": "done"}, headers=_AUTH)
    assert r.status_code == 200
    admin.get_case.assert_awaited_once_with(case_id="LB-C7", session_cookie="cookie")
    admin.patch_case.assert_awaited_once_with(case_id="case-uuid", body={"status": "done"}, session_cookie="cookie")


@pytest.mark.real_fences
def test_attachment_content_streams_only_this_cases_asset(client, monkeypatch):
    admin = MagicMock()
    admin.get_case = AsyncMock(
        return_value={"id": "c1", "companyId": "co_abc", "attachments": [{"id": "att1", "asset": {"id": "asset9"}}]}
    )
    admin.open_asset_stream = AsyncMock(return_value=_Upstream())
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/cases/c1/attachments/att1/content", headers=_AUTH)
    assert r.status_code == 200 and r.content == b"png"
    admin.open_asset_stream.assert_awaited_once_with(asset_id="asset9", session_cookie="cookie")

    admin.open_asset_stream.reset_mock()
    assert client.get("/api/v1/teams/cases/c1/attachments/asset9/content", headers=_AUTH).status_code == 404
    admin.open_asset_stream.assert_not_awaited()
