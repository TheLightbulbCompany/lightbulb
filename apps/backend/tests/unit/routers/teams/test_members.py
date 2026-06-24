"""Tests for the Teams BFF — Members roster (Clerk-enriched).

The ``GET /teams/members`` route joins each upstream membership row's
``principalId`` back to Clerk (via the same principal→Clerk map the activity
feed uses) and resolves name/email/imageUrl. The picker only wants resolvable
humans, so a row whose Clerk lookup fails is dropped rather than shipped raw.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="clerk_a",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_a",
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


def _clerk_user(user_id: str, first: str, last: str, email: str) -> dict:
    return {
        "id": user_id,
        "first_name": first,
        "last_name": last,
        "image_url": f"https://img/{user_id}",
        "primary_email_address_id": "ea_1",
        "email_addresses": [{"id": "ea_1", "email_address": email}],
    }


def test_list_members_enriches_each_row_with_clerk_identity(client, monkeypatch):
    admin = MagicMock()
    admin.list_members = AsyncMock(
        return_value=[{"id": "m1", "principalId": "pcu_a"}, {"id": "m2", "principalId": "pcu_b"}]
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(
        return_value=[
            {"user_id": "clerk_a", "paperclip_user_id": "pcu_a"},
            {"user_id": "clerk_b", "paperclip_user_id": "pcu_b"},
        ]
    )

    clerk_users = {
        "clerk_a": _clerk_user("clerk_a", "Ada", "Lovelace", "ada@x.co"),
        "clerk_b": _clerk_user("clerk_b", "Alan", "Turing", "alan@x.co"),
    }

    async def fake_get_user(user_id: str):
        return clerk_users.get(user_id)

    from routers.teams import agents as agents_mod
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    members = r.json()["members"]
    assert members == [
        {
            "userId": "clerk_a",
            "name": "Ada Lovelace",
            "email": "ada@x.co",
            "imageUrl": "https://img/clerk_a",
        },
        {
            "userId": "clerk_b",
            "name": "Alan Turing",
            "email": "alan@x.co",
            "imageUrl": "https://img/clerk_b",
        },
    ]
    admin.list_members.assert_awaited_once_with(company_id="co_abc", session_cookie="cookie")


def test_list_members_drops_rows_with_no_clerk_match(client, monkeypatch):
    """A principal not in the org map (drift) or with no Clerk user is dropped,
    never shipped as a raw id — the picker only wants resolvable humans."""
    admin = MagicMock()
    admin.list_members = AsyncMock(
        return_value=[{"id": "m1", "principalId": "pcu_a"}, {"id": "m2", "principalId": "pcu_orphan"}]
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_a", "paperclip_user_id": "pcu_a"}])

    async def fake_get_user(user_id: str):
        return _clerk_user("clerk_a", "Ada", "Lovelace", "ada@x.co")

    from routers.teams import agents as agents_mod
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    members = r.json()["members"]
    assert [m["userId"] for m in members] == ["clerk_a"]


def test_list_members_tolerates_clerk_failure(client, monkeypatch):
    """A Clerk lookup that raises drops that member and never 500s the route."""
    admin = MagicMock()
    admin.list_members = AsyncMock(return_value=[{"id": "m1", "principalId": "pcu_a"}])
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_a", "paperclip_user_id": "pcu_a"}])

    async def fake_get_user(user_id: str):
        raise RuntimeError("clerk down")

    from routers.teams import agents as agents_mod
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

    r = client.get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["members"] == []


def test_list_members_personal_context_uses_self_pairing(monkeypatch):
    """A personal owner (org_id None) skips the org GSI and maps their own
    paperclip principal → their Clerk id."""
    personal_ctx = TeamsContext(
        user_id="clerk_solo",
        org_id=None,
        owner_id="clerk_solo",
        company_id="co_solo",
        paperclip_user_id="pcu_solo",
        session_cookie="cookie",
    )

    from routers.teams import agents as agents_mod
    import core.services.clerk_admin as clerk_admin

    async def fake_ctx():
        return personal_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        admin = MagicMock()
        admin.list_members = AsyncMock(return_value=[{"id": "m1", "principalId": "pcu_solo"}])
        repo = MagicMock()
        repo.list_by_org_id = AsyncMock(return_value=[])

        async def fake_get_user(user_id: str):
            return _clerk_user("clerk_solo", "Grace", "Hopper", "grace@x.co")

        monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
        monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
        monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

        r = TestClient(app).get("/api/v1/teams/members", headers={"Authorization": "Bearer x"})
        assert r.status_code == 200
        members = r.json()["members"]
        assert members == [
            {
                "userId": "clerk_solo",
                "name": "Grace Hopper",
                "email": "grace@x.co",
                "imageUrl": "https://img/clerk_solo",
            }
        ]
        # Personal context never queries the org GSI.
        repo.list_by_org_id.assert_not_called()
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)
