"""Tests for the Teams BFF — Members roster.

``GET /teams/members`` lists the humans in the caller's workspace from Clerk
org membership — the same set the messaging service accepts as room members —
so the new-conversation picker can never hide someone the backend would let
you add. Paperclip's own member rows are NOT consulted (a teammate whose
Paperclip membership-add never landed used to vanish from the picker).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from main import app


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="clerk_a", org_id="o1")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def workspace_repo(monkeypatch):
    """Workspace o1 holds clerk_a (Paperclip identity pcu_a) and clerk_leo
    (joined in Clerk, no Paperclip identity yet)."""
    from types import SimpleNamespace

    from routers.teams import agents as agents_mod

    identities = {"clerk_a": SimpleNamespace(paperclip_user_id="pcu_a")}
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=SimpleNamespace(member_user_ids={"clerk_a", "clerk_leo"}))
    repo.get_identity = AsyncMock(side_effect=lambda uid: identities.get(uid))
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    return repo


def _org_member(user_id: str, first: str, last: str, email: str) -> dict:
    return {
        "user_id": user_id,
        "first_name": first,
        "last_name": last,
        "email": email,
        "image_url": f"https://img/{user_id}",
        "role": "org:member",
    }


def test_list_members_is_the_clerk_org_roster(client, monkeypatch):
    """Every Clerk org member is listed — including one Paperclip has never
    heard of — and Paperclip's member list is never consulted."""
    admin = MagicMock()
    admin.list_members = AsyncMock(return_value=[{"id": "m1", "principalId": "pcu_a"}])
    org_members = AsyncMock(
        return_value=[
            _org_member("clerk_a", "Ada", "Lovelace", "ada@x.co"),
            # No name at all → the label falls back to the email.
            _org_member("clerk_leo", "", "", "leo@x.co"),
        ]
    )

    from routers.teams import agents as agents_mod
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(clerk_admin, "list_organization_members", org_members)

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["members"] == [
        # paperclipUserId is what a task's assigneeUserId holds; a member with
        # no Paperclip identity yet carries null and cannot own a task.
        {
            "userId": "clerk_a",
            "name": "Ada Lovelace",
            "email": "ada@x.co",
            "imageUrl": "https://img/clerk_a",
            "paperclipUserId": "pcu_a",
        },
        {
            "userId": "clerk_leo",
            "name": "leo@x.co",
            "email": "leo@x.co",
            "imageUrl": "https://img/clerk_leo",
            "paperclipUserId": None,
        },
    ]
    org_members.assert_awaited_once_with("o1")
    admin.list_members.assert_not_called()


def test_list_members_empty_when_clerk_unavailable(client, monkeypatch):
    """The Clerk helper degrades to [] on failure; the route ships that, never a 500."""
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=[]))

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["members"] == []


def test_list_members_personal_context_is_just_the_caller(monkeypatch):
    """A personal owner (org_id None) has no org to list — the roster is
    themselves, resolved from Clerk, and the org endpoint is never called."""
    import core.services.clerk_admin as clerk_admin

    async def fake_get_user(user_id: str):
        assert user_id == "clerk_solo"
        return {
            "id": user_id,
            "first_name": "Grace",
            "last_name": "Hopper",
            "image_url": "https://img/clerk_solo",
            "primary_email_address_id": "ea_1",
            "email_addresses": [{"id": "ea_1", "email_address": "grace@x.co"}],
        }

    org_members = AsyncMock(return_value=[])
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="clerk_solo", org_id=None)
    try:
        monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
        monkeypatch.setattr(clerk_admin, "list_organization_members", org_members)

        r = TestClient(app).get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
        assert r.status_code == 200
        assert r.json()["members"] == [
            {
                "userId": "clerk_solo",
                "name": "Grace Hopper",
                "email": "grace@x.co",
                "imageUrl": "https://img/clerk_solo",
                "paperclipUserId": None,
            }
        ]
        org_members.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
