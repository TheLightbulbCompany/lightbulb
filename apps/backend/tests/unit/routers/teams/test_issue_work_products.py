# apps/backend/tests/unit/routers/teams/test_issue_work_products.py
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


def test_list_work_products_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_work_products = AsyncMock(
        return_value=[
            {"id": "wp1", "type": "preview_url", "title": "Preview"},
            {"id": "wp2", "type": "pull_request", "title": "PR"},
        ]
    )
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/issues/iss1/work-products",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "workProducts": [
            {"id": "wp1", "type": "preview_url", "title": "Preview"},
            {"id": "wp2", "type": "pull_request", "title": "PR"},
        ]
    }
    admin.list_issue_work_products.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")


def test_create_work_product_forwards_camelcase_body(client, monkeypatch):
    admin = MagicMock()
    admin.create_issue_work_product = AsyncMock(return_value={"id": "wp_new"})
    _install_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/work-products",
        headers={"Authorization": "Bearer x"},
        json={
            "type": "preview_url",
            "provider": "paperclip",
            "title": "Deploy preview",
            "url": "https://preview.example.com",
            "reviewState": "needs_board_review",
            "isPrimary": True,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"id": "wp_new"}
    # ``exclude_none`` drops unset optionals; ``by_alias`` emits camelCase.
    admin.create_issue_work_product.assert_awaited_once_with(
        issue_id="iss1",
        session_cookie="cookie",
        body={
            "type": "preview_url",
            "provider": "paperclip",
            "title": "Deploy preview",
            "url": "https://preview.example.com",
            "reviewState": "needs_board_review",
            "isPrimary": True,
        },
    )


def test_create_work_product_rejects_unknown_field(client, monkeypatch):
    admin = MagicMock()
    admin.create_issue_work_product = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    # ``adapterType`` is a smuggled field — _Strict (extra="forbid") 422s it
    # at the FastAPI boundary before any upstream call.
    r = client.post(
        "/api/v1/teams/issues/iss1/work-products",
        headers={"Authorization": "Bearer x"},
        json={
            "type": "preview_url",
            "provider": "paperclip",
            "title": "x",
            "adapterType": "openclaw_gateway",
        },
    )
    assert r.status_code == 422
    admin.create_issue_work_product.assert_not_awaited()


def test_create_work_product_rejects_bad_type(client, monkeypatch):
    admin = MagicMock()
    admin.create_issue_work_product = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/work-products",
        headers={"Authorization": "Bearer x"},
        json={"type": "malware", "provider": "paperclip", "title": "x"},
    )
    assert r.status_code == 422
    admin.create_issue_work_product.assert_not_awaited()


def test_patch_work_product_review_state_forwards(client, monkeypatch):
    admin = MagicMock()
    admin.update_work_product = AsyncMock(return_value={"id": "wp1", "reviewState": "approved"})
    _install_admin(monkeypatch, admin)

    r = client.patch(
        "/api/v1/teams/work-products/wp1",
        headers={"Authorization": "Bearer x"},
        json={"reviewState": "approved"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "wp1", "reviewState": "approved"}
    # ``exclude_unset`` forwards only the key the caller set.
    admin.update_work_product.assert_awaited_once_with(
        work_product_id="wp1",
        session_cookie="cookie",
        body={"reviewState": "approved"},
    )


def test_patch_work_product_rejects_bad_review_state(client, monkeypatch):
    admin = MagicMock()
    admin.update_work_product = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.patch(
        "/api/v1/teams/work-products/wp1",
        headers={"Authorization": "Bearer x"},
        json={"reviewState": "not_a_state"},
    )
    assert r.status_code == 422
    admin.update_work_product.assert_not_awaited()


def test_list_company_artifacts_forwards_query_and_company_from_ctx(client, monkeypatch):
    admin = MagicMock()
    admin.list_company_artifacts = AsyncMock(return_value={"artifacts": [], "groups": [], "nextCursor": None})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/artifacts?groupBy=task&kind=image&limit=10",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"artifacts": [], "groups": [], "nextCursor": None}
    # Company id comes from the server-derived ctx, never a client param.
    admin.list_company_artifacts.assert_awaited_once_with(
        session_cookie="cookie",
        company_id="co_abc",
        params={"kind": "image", "groupBy": "task", "limit": "10"},
    )


def test_list_company_artifacts_rejects_bad_group_by(client, monkeypatch):
    admin = MagicMock()
    admin.list_company_artifacts = AsyncMock(return_value={})
    _install_admin(monkeypatch, admin)

    r = client.get(
        "/api/v1/teams/artifacts?groupBy=everything",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.list_company_artifacts.assert_not_awaited()
