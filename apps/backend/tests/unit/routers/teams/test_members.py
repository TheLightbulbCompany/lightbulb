"""Tests for the Teams BFF — Members roster.

``GET /teams/members`` lists the humans in the caller's workspace: exactly one, the caller, resolved from
Clerk — the same set the messaging service accepts as room members — so the new-conversation picker can
never hide someone the backend would let you add. Paperclip's own member rows are NOT consulted.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from main import app


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="o1", human_session=True)
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


def test_list_members_empty_when_clerk_unavailable(client, monkeypatch):
    """Clerk has no such user: the route ships an empty roster, never a 500."""
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(clerk_admin, "get_user", AsyncMock(return_value=None))

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["members"] == []


def test_list_members_is_just_the_caller(monkeypatch):
    """The roster is the caller themselves, resolved from Clerk."""
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

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="clerk_solo", human_session=True)
    try:
        monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

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
    finally:
        app.dependency_overrides.pop(get_current_user, None)
