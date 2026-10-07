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
    # The ``me`` sentinel is resolved to the caller's real Paperclip user id
    # (ctx.paperclip_user_id) — upstream compares this as a literal id.
    assert kwargs["params"]["assigneeUserId"] == "pcu"


def test_search_resolves_assignee_me_to_caller_paperclip_user_id(client, monkeypatch):
    """``assigneeUserId=me`` forwards the caller's real id, not the sentinel."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?assigneeUserId=me",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    # Sentinel translated to ctx.paperclip_user_id ("pcu"), NOT the literal "me".
    assert kwargs["params"]["assigneeUserId"] == "pcu"


def test_search_case_insensitive_assignee_me_sentinel(client, monkeypatch):
    """A mixed-case ``ME`` sentinel is still resolved to the caller id."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?assigneeUserId=Me",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["params"]["assigneeUserId"] == "pcu"


def test_search_real_assignee_user_id_passes_through(client, monkeypatch):
    """A concrete user id is forwarded verbatim (only ``me`` is translated)."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    uid = "11111111-2222-3333-4444-555555555555"
    r = client.get(
        f"/api/v1/teams/search?assigneeUserId={uid}",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["params"]["assigneeUserId"] == uid


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


def test_search_rejects_unparseable_updated_after(client, monkeypatch):
    """A malformed ``updatedAfter`` 4xxs upstream and the error handler leaks the
    internal ``/api/companies/{id}/search`` path — so reject it locally with a
    clean 422 and never proxy."""
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?updatedAfter=not-a-date",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    # Local error body — no company-scoped upstream path leaked.
    assert "companies" not in r.text
    admin.search_company.assert_not_awaited()


def test_search_accepts_valid_updated_after(client, monkeypatch):
    admin = MagicMock()
    admin.search_company = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search?updatedAfter=2026-01-02T03:04:05Z",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company.call_args
    assert kwargs["params"]["updatedAfter"] == "2026-01-02T03:04:05Z"


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


def test_search_extract_rejects_all_whitespace_contains(client, monkeypatch):
    """``contains`` is length-checked AFTER trimming — an all-whitespace value
    would pass a raw-length floor, 4xx upstream, and leak the company-scoped
    path via the global error handler. Reject it locally, never proxy."""
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    # Two spaces: 2 chars raw, 0 after trim.
    r = client.get(
        "/api/v1/teams/search/extract?contains=%20%20",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    assert "companies" not in r.text
    admin.search_company_extract.assert_not_awaited()


def test_search_extract_rejects_single_char_after_trim(client, monkeypatch):
    """A value that trims to a single char is below the min and must 422
    locally without reaching the upstream proxy."""
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    # Space + single char: 2 chars raw, 1 after trim.
    r = client.get(
        "/api/v1/teams/search/extract?contains=%20a",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    assert "companies" not in r.text
    admin.search_company_extract.assert_not_awaited()


def test_search_extract_accepts_padded_contains_with_enough_chars(client, monkeypatch):
    """Surrounding whitespace that still leaves >=2 chars after trim passes
    through to the upstream proxy."""
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    # "  ab " -> 5 chars raw, 2 after trim.
    r = client.get(
        "/api/v1/teams/search/extract?contains=%20%20ab%20",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.search_company_extract.assert_awaited_once()


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


def test_search_extract_rejects_unparseable_updated_after(client, monkeypatch):
    """Same leak as ``/search`` — a malformed ``updatedAfter`` must 422 locally
    and never reach the upstream proxy."""
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=hello&updatedAfter=not-a-date",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    assert "companies" not in r.text
    admin.search_company_extract.assert_not_awaited()


def test_search_extract_accepts_valid_updated_after(client, monkeypatch):
    admin = MagicMock()
    admin.search_company_extract = AsyncMock(return_value={"results": []})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/search/extract?contains=hello&updatedAfter=2026-01-02T03:04:05Z",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    _, kwargs = admin.search_company_extract.call_args
    assert kwargs["params"]["updatedAfter"] == "2026-01-02T03:04:05Z"
