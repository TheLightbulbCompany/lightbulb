# apps/backend/tests/unit/routers/teams/test_search.py
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


# --------------------------------------------------------------------------
# GET /search
# --------------------------------------------------------------------------


def test_search_forwards_company_from_ctx_and_returns_verbatim(client, monkeypatch):
    upstream = {
        "query": "invoice",
        "results": [{"id": "i1", "type": "issue", "title": "Invoice bug"}],
        "countsByType": {"issue": 1},
        "hasMore": False,
    }
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value=upstream)
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?q=invoice&scope=issues&sort=updated&limit=10&offset=5",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    # Upstream response returned verbatim (already an envelope object).
    assert r.json() == upstream
    # Company id comes from the server-derived ctx, never a client param.
    admin.search_company.assert_awaited_once_with(
        session_cookie="cookie",
        company_id="co_abc",
        params={
            "q": "invoice",
            "scope": "issues",
            "sort": "updated",
            "limit": "10",
            "offset": "5",
        },
    )


def test_search_ignores_client_supplied_company_id(client, monkeypatch):
    """A client cannot redirect the search at another company (IDOR)."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?q=x&companyId=co_victim&company_id=co_victim",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["company_id"] == "co_abc"
    # The smuggled params never reach upstream.
    assert "companyId" not in kwargs["params"]
    assert "company_id" not in kwargs["params"]


def test_search_forwards_multi_value_status_and_priority(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?q=x&status=todo&status=in_progress&priority=high",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["params"]["status"] == ["todo", "in_progress"]
    assert kwargs["params"]["priority"] == ["high"]


def test_search_accepts_comma_separated_status(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?status=todo,done,todo",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    # Comma-split + de-duped.
    assert kwargs["params"]["status"] == ["todo", "done"]


def test_search_rejects_bad_scope(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?scope=everything",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company.assert_not_awaited()


def test_search_rejects_bad_sort(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?sort=sideways",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company.assert_not_awaited()


def test_search_rejects_bad_status_value(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?status=not_a_status",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company.assert_not_awaited()


def test_search_rejects_non_uuid_project_filter(client, monkeypatch):
    """A malformed projectId would 500 + leak the company path upstream."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?projectId=not-a-uuid",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company.assert_not_awaited()


def test_search_allows_assignee_sentinels(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?assigneeAgentId=null&assigneeUserId=me",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["params"]["assigneeAgentId"] == "null"
    assert kwargs["params"]["assigneeUserId"] == "me"


def test_search_rejects_bad_limit(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?limit=999",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company.assert_not_awaited()


def test_search_no_params_forwards_none(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/search", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.search_company.assert_awaited_once_with(session_cookie="cookie", company_id="co_abc", params=None)


# --------------------------------------------------------------------------
# GET /search/extract
# --------------------------------------------------------------------------


def test_search_extract_forwards_and_returns_verbatim(client, monkeypatch):
    upstream = {"contains": "http", "results": [], "hasMore": False, "truncated": False}
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value=upstream)
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=http&kind=url&scope=comments&limit=50&matchesPerIssue=5",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == upstream
    admin.search_company_extract.assert_awaited_once_with(
        session_cookie="cookie",
        company_id="co_abc",
        params={
            "contains": "http",
            "kind": "url",
            "scope": "comments",
            "limit": "50",
            "matchesPerIssue": "5",
        },
    )


def test_search_extract_requires_min_length_contains(client, monkeypatch):
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=x",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company_extract.assert_not_awaited()


def test_search_extract_rejects_both_updated_filters(client, monkeypatch):
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=hello&updatedWithin=7d&updatedAfter=2026-01-01",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company_extract.assert_not_awaited()


def test_search_extract_rejects_bad_kind(client, monkeypatch):
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=hello&kind=fuzzy",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.search_company_extract.assert_not_awaited()
