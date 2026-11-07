"""/teams/settings reads and writes the workspace META row; Paperclip is never called."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams import agents as agents_mod
from routers.teams import settings as mod
from routers.teams.deps import TeamsContext


@pytest.fixture
def client(monkeypatch):
    ctx = TeamsContext(
        user_id="u1", org_id="org_1", owner_id="org_1", company_id="", paperclip_user_id="", session_cookie=""
    )

    async def fake_ctx():
        return ctx

    def no_paperclip():
        raise AssertionError("Paperclip called")

    monkeypatch.setattr(agents_mod, "_admin", no_paperclip)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_settings_get_patch_roundtrip_no_paperclip(client, monkeypatch):
    ws = SimpleNamespace(name=None, description=None)

    async def set_profile(self, org_id, *, name, description):
        assert org_id == "org_1"
        ws.name = name if name is not None else ws.name
        ws.description = description if description is not None else ws.description

    monkeypatch.setattr(mod.PaperclipRepo, "get_workspace", AsyncMock(return_value=ws))
    monkeypatch.setattr(mod.PaperclipRepo, "set_profile", set_profile)
    monkeypatch.setattr(
        mod.workspace_name.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Clerk Co"})
    )
    monkeypatch.setattr(mod.workspace_name.PaperclipRepo, "get_workspace", AsyncMock(return_value=ws))
    h = {"Authorization": "Bearer x"}

    assert client.get("/api/v1/teams/settings", headers=h).json() == {"name": "Clerk Co", "description": ""}
    r = client.patch("/api/v1/teams/settings", json={"name": "Acme", "description": "anvils"}, headers=h)
    assert r.status_code == 200 and r.json() == {"name": "Acme", "description": "anvils"}
    assert client.get("/api/v1/teams/settings", headers=h).json() == {"name": "Acme", "description": "anvils"}


def test_patch_settings_rejects_unknown_field(client):
    """Unknown fields (eg ``status``) must be 422'd by the body whitelist."""
    r = client.patch(
        "/api/v1/teams/settings", json={"name": "x", "status": "archived"}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 422


def test_patch_settings_rejects_legacy_display_name(client):
    """``displayName`` was an old (broken) key: it fails loudly at the boundary."""
    r = client.patch("/api/v1/teams/settings", json={"displayName": "Acme Inc."}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 422
