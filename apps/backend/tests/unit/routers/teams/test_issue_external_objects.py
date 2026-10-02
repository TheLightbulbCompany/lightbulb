"""Teams BFF — external objects (the GitHub PRs / issues an issue mentions)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
OBJECT = "22222222-2222-2222-2222-222222222222"
H = {"Authorization": "Bearer x"}
BASE = f"/api/v1/teams/issues/{ISSUE}/external-objects"
SUMMARIES = "/api/v1/teams/issues/external-object-summaries"


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
    fake.list_issue_external_objects = AsyncMock(return_value=[{"object": {"id": OBJECT}, "mentions": []}])
    fake.refresh_issue_external_objects = AsyncMock(return_value={"refreshed": []})
    fake.list_external_object_summaries = AsyncMock(return_value={"summaries": {ISSUE: {"total": 1}}})
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def test_list_wraps_the_bare_array_and_uses_the_member_session(admin):
    r = client.get(BASE, headers=H)
    assert r.status_code == 200
    assert r.json() == {"externalObjects": [{"object": {"id": OBJECT}, "mentions": []}]}
    admin.list_issue_external_objects.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_flag_off_upstream_is_an_empty_list_not_an_error(admin):
    admin.list_issue_external_objects.return_value = []
    r = client.get(BASE, headers=H)
    assert r.status_code == 200
    assert r.json() == {"externalObjects": []}


def test_refresh_forwards_camelcase_body(admin):
    r = client.post(f"{BASE}/refresh", json={"objectIds": [OBJECT]}, headers=H)
    assert r.status_code == 200
    admin.refresh_issue_external_objects.assert_awaited_once_with(
        issue_id=ISSUE, body={"objectIds": [OBJECT]}, session_cookie="cookie"
    )


def test_summaries_are_scoped_to_the_callers_company_never_a_body_field(admin):
    r = client.post(SUMMARIES, json={"issueIds": [ISSUE]}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"summaries": {ISSUE: {"total": 1}}}
    admin.list_external_object_summaries.assert_awaited_once_with(
        company_id="co_abc", issue_ids=[ISSUE], session_cookie="cookie"
    )


@pytest.mark.parametrize(
    "url,body",
    [
        (f"{BASE}/refresh", {"objectIds": ["not-a-uuid"]}),
        (f"{BASE}/refresh", {"objectIds": [OBJECT] * 51}),
        (f"{BASE}/refresh", {"companyId": "co_other"}),
        (SUMMARIES, {"issueIds": ["not-a-uuid"]}),
        (SUMMARIES, {"issueIds": [ISSUE], "companyId": "co_other"}),
        (SUMMARIES, {}),
    ],
)
def test_off_schema_bodies_are_422_and_never_forwarded(admin, url, body):
    assert client.post(url, json=body, headers=H).status_code == 422
    admin.refresh_issue_external_objects.assert_not_awaited()
    admin.list_external_object_summaries.assert_not_awaited()


@pytest.mark.real_fences
@pytest.mark.parametrize("method", ["list", "refresh"])
def test_another_companys_issue_is_404_and_never_forwarded(admin, method):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    r = client.get(BASE, headers=H) if method == "list" else client.post(f"{BASE}/refresh", json={}, headers=H)
    assert r.status_code == 404
    admin.list_issue_external_objects.assert_not_awaited()
    admin.refresh_issue_external_objects.assert_not_awaited()
