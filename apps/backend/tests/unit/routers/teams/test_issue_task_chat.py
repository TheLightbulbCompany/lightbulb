"""Teams BFF — the ported task chat's routes (live runs, active run, comment delete)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services.paperclip_admin_client import PaperclipApiError
from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
COMMENT = "33333333-3333-3333-3333-333333333333"
RUN = "44444444-4444-4444-4444-444444444444"
H = {"Authorization": "Bearer x"}


@pytest.fixture
def admin(monkeypatch):
    from routers.teams import agents as agents_mod

    ctx = TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu",
        session_cookie="cookie",
    )

    async def fake_ctx():
        return ctx

    fake = MagicMock()
    fake.get_issue = AsyncMock(return_value={"id": ISSUE, "companyId": "co_abc"})
    fake.list_issue_live_runs = AsyncMock(return_value=[{"id": RUN, "status": "running"}])
    fake.get_issue_active_run = AsyncMock(return_value={"id": RUN, "status": "running"})
    fake.delete_issue_comment = AsyncMock(return_value={"id": COMMENT, "deletedAt": "2026-09-24T00:00:00Z"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def _call(route):
    if route == "live-runs":
        return client.get(f"/api/v1/teams/issues/{ISSUE}/live-runs", headers=H)
    if route == "active-run":
        return client.get(f"/api/v1/teams/issues/{ISSUE}/active-run", headers=H)
    return client.delete(f"/api/v1/teams/issues/{ISSUE}/comments/{COMMENT}", headers=H)


def test_live_runs_forwards_with_member_session(admin):
    r = _call("live-runs")
    assert r.status_code == 200
    assert r.json() == [{"id": RUN, "status": "running"}]
    admin.list_issue_live_runs.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_active_run_forwards_and_passes_null_through(admin):
    admin.get_issue_active_run.return_value = None
    r = _call("active-run")
    assert r.status_code == 200
    assert r.json() is None
    admin.get_issue_active_run.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_delete_comment_forwards_without_mode(admin):
    r = client.delete(f"/api/v1/teams/issues/{ISSUE}/comments/{COMMENT}?mode=cancel", headers=H)
    assert r.status_code == 200
    admin.delete_issue_comment.assert_awaited_once_with(issue_id=ISSUE, comment_id=COMMENT, session_cookie="cookie")


@pytest.mark.parametrize("route", ["live-runs", "active-run", "delete"])
def test_another_companys_issue_is_404_and_never_forwarded(admin, route):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    assert _call(route).status_code == 404
    admin.list_issue_live_runs.assert_not_awaited()
    admin.get_issue_active_run.assert_not_awaited()
    admin.delete_issue_comment.assert_not_awaited()


def test_delete_rejects_non_uuid_comment_id(admin):
    r = client.delete(f"/api/v1/teams/issues/{ISSUE}/comments/not-a-uuid", headers=H)
    assert r.status_code == 422
    admin.delete_issue_comment.assert_not_awaited()


def test_delete_surfaces_paperclips_non_author_403(admin):
    admin.delete_issue_comment.side_effect = PaperclipApiError(
        "DELETE /api/issues/x/comments/y -> 403",
        403,
        {"error": "Only the comment author can delete comments"},
    )
    r = _call("delete")
    assert r.status_code == 403
    assert r.json()["detail"] == "Only the comment author can delete comments"


def test_smuggled_query_in_issue_id_is_rejected_before_the_fence(admin):
    # uvicorn decodes %3F/%26 into the path segment; without the id pattern this
    # reached get_issue and forwarded DELETE /api/issues/<id>?mode=cancel&/comments/<cid>.
    r = client.delete(f"/api/v1/teams/issues/{ISSUE}%3Fmode=cancel%26/comments/{COMMENT}", headers=H)
    assert r.status_code == 422
    admin.get_issue.assert_not_awaited()
    admin.delete_issue_comment.assert_not_awaited()


def test_smuggled_query_in_run_id_is_rejected(admin):
    admin.cancel_run = AsyncMock()
    r = client.post(f"/api/v1/teams/heartbeat-runs/{RUN}%3Fx=1%26/cancel", headers=H)
    assert r.status_code == 422
    admin.cancel_run.assert_not_awaited()
