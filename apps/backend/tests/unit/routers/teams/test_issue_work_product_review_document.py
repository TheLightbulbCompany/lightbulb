"""Teams BFF — work-product review-document (the ported issue Output section's Markdown preview)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
WP = "22222222-2222-2222-2222-222222222222"
H = {"Authorization": "Bearer x"}
URL = f"/api/v1/teams/issues/{ISSUE}/work-products/{WP}/review-document"


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
    fake.ensure_work_product_review_document = AsyncMock(
        return_value={"id": "doc1", "key": f"artifact-review-{WP}", "body": "# Hi"}
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def test_forwards_with_member_session(admin):
    r = client.post(URL, headers=H)
    assert r.status_code == 200
    assert r.json()["key"] == f"artifact-review-{WP}"
    admin.ensure_work_product_review_document.assert_awaited_once_with(
        session_cookie="cookie", issue_id=ISSUE, work_product_id=WP
    )


def test_another_companys_issue_is_404_and_never_forwarded(admin):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    assert client.post(URL, headers=H).status_code == 404
    admin.ensure_work_product_review_document.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        f"/api/v1/teams/issues/{ISSUE}/work-products/bad.id/review-document",
        f"/api/v1/teams/issues/bad.id/work-products/{WP}/review-document",
    ],
)
def test_bad_ids_are_422_and_never_forwarded(admin, url):
    assert client.post(url, headers=H).status_code == 422
    admin.get_issue.assert_not_awaited()
    admin.ensure_work_product_review_document.assert_not_awaited()
