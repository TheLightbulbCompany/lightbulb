"""Tests for the Teams Settings BFF.

PATCH /settings is whitelisted to ``display_name`` + ``description``;
unknown fields are 422'd at the FastAPI boundary.
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


def test_get_settings_returns_company(client, monkeypatch):
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"id": "co_abc", "name": "Acme", "description": "..."})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/settings",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_company.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_patch_settings_passes_whitelisted_body(client, monkeypatch):
    admin = MagicMock()
    admin.patch_company = AsyncMock(return_value={"id": "co_abc", "name": "Acme"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/settings",
        json={"display_name": "Acme Inc.", "description": "tighter copy"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.patch_company.await_args.kwargs["body"]
    assert body == {"displayName": "Acme Inc.", "description": "tighter copy"}


def test_patch_settings_rejects_unknown_field(client):
    """Unknown fields (eg ``status``) must be 422'd by the body whitelist."""
    r = client.patch(
        "/api/v1/teams/settings",
        json={"display_name": "x", "status": "archived"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
