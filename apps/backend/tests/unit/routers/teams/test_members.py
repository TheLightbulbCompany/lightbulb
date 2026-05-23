"""Tests for the Teams Members BFF.

Members endpoint joins Paperclip ``companyMemberships`` with Clerk
user info via ``_resolve_user_email`` (re-exported from
``routers.teams.agents``).

Email enrichment goes through a Paperclip principalId → Clerk user_id
map built from the org's ``paperclip-companies`` rows, since
``_resolve_user_email`` calls Clerk ``get_user(user_id)`` and Clerk
doesn't know about Paperclip principalIds.
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


def _stub_repo_with_org_rows(rows: list[dict]):
    """Build a MagicMock repo whose ``list_by_org_id`` returns ``rows``."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=rows)
    return repo


def test_list_members_resolves_email_via_clerk_id_map(client, monkeypatch):
    """Each upstream member row should be enriched with
    ``email_via_clerk`` resolved via the Paperclip→Clerk principalId
    map (NOT by passing the principalId straight to Clerk)."""
    admin = MagicMock()
    admin.list_members = AsyncMock(
        return_value={
            "members": [
                {"id": "m1", "principalId": "pcu_aaa", "role": "owner"},
                {"id": "m2", "principalId": "pcu_bbb", "role": "member"},
            ]
        }
    )

    repo = _stub_repo_with_org_rows(
        [
            {"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"},
            {"user_id": "clerk_bbb", "paperclip_user_id": "pcu_bbb"},
        ]
    )

    resolve_calls: list[str] = []

    async def fake_resolve(user_id: str) -> str:
        resolve_calls.append(user_id)
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get(
        "/api/v1/teams/members",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "members": [
            {
                "id": "m1",
                "principalId": "pcu_aaa",
                "role": "owner",
                "email_via_clerk": "clerk_aaa@example.com",
            },
            {
                "id": "m2",
                "principalId": "pcu_bbb",
                "role": "member",
                "email_via_clerk": "clerk_bbb@example.com",
            },
        ]
    }
    # Critical: _resolve_user_email is called with CLERK ids, never
    # Paperclip principalIds.
    assert resolve_calls == ["clerk_aaa", "clerk_bbb"]
    repo.list_by_org_id.assert_awaited_once_with("o1")
    admin.list_members.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_list_members_normalizes_flat_list_payload(client, monkeypatch):
    """If Paperclip returns the flat-list shape (no ``members`` envelope),
    the endpoint must not raise AttributeError on ``.get`` — it should
    iterate the list directly, mirroring
    ``paperclip_provisioning.archive_member``'s defensive shape check.
    """
    admin = MagicMock()
    admin.list_members = AsyncMock(
        return_value=[
            {"id": "m1", "principalId": "pcu_aaa"},
            {"id": "m2", "principalId": "pcu_bbb"},
        ]
    )

    repo = _stub_repo_with_org_rows(
        [
            {"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"},
            {"user_id": "clerk_bbb", "paperclip_user_id": "pcu_bbb"},
        ]
    )

    async def fake_resolve(user_id: str) -> str:
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get(
        "/api/v1/teams/members",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "members": [
            {
                "id": "m1",
                "principalId": "pcu_aaa",
                "email_via_clerk": "clerk_aaa@example.com",
            },
            {
                "id": "m2",
                "principalId": "pcu_bbb",
                "email_via_clerk": "clerk_bbb@example.com",
            },
        ]
    }


def test_list_members_unmapped_principal_yields_none_email(client, monkeypatch):
    """If a member's principalId is NOT in the org's paperclip→clerk
    map (e.g. drift between Paperclip and our DDB), the row should
    still ship with ``email_via_clerk: None`` — no exception, no
    accidental Clerk lookup against the Paperclip id.
    """
    admin = MagicMock()
    admin.list_members = AsyncMock(
        return_value={
            "members": [
                {"id": "m1", "principalId": "pcu_known"},
                {"id": "m2", "principalId": "pcu_orphan"},
            ]
        }
    )

    repo = _stub_repo_with_org_rows(
        [
            {"user_id": "clerk_known", "paperclip_user_id": "pcu_known"},
        ]
    )

    resolve_calls: list[str] = []

    async def fake_resolve(user_id: str) -> str:
        resolve_calls.append(user_id)
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get(
        "/api/v1/teams/members",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "members": [
            {
                "id": "m1",
                "principalId": "pcu_known",
                "email_via_clerk": "clerk_known@example.com",
            },
            {
                "id": "m2",
                "principalId": "pcu_orphan",
                "email_via_clerk": None,
            },
        ]
    }
    # Must not have called Clerk for the orphan principal.
    assert resolve_calls == ["clerk_known"]


def test_list_members_tolerates_clerk_failure(client, monkeypatch):
    """If Clerk lookup raises, the member row still ships with
    ``email_via_clerk: None`` instead of failing the whole request."""
    admin = MagicMock()
    admin.list_members = AsyncMock(return_value={"members": [{"id": "m1", "principalId": "pcu_aaa"}]})

    repo = _stub_repo_with_org_rows([{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    async def fake_resolve(user_id: str) -> str:
        raise RuntimeError("clerk down")

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get(
        "/api/v1/teams/members",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "members": [
            {
                "id": "m1",
                "principalId": "pcu_aaa",
                "email_via_clerk": None,
            }
        ]
    }


def test_list_members_personal_context_skips_org_lookup(client, monkeypatch):
    """For personal users (``org_id is None``) we shouldn't query the
    GSI — the caller is the sole member, and we map their own
    paperclip_user_id → user_id directly from the context."""
    personal_ctx = TeamsContext(
        user_id="clerk_solo",
        org_id=None,
        owner_id="clerk_solo",
        company_id="co_solo",
        paperclip_user_id="pcu_solo",
        session_cookie="cookie",
    )

    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return personal_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        admin = MagicMock()
        admin.list_members = AsyncMock(return_value={"members": [{"id": "m1", "principalId": "pcu_solo"}]})
        repo = MagicMock()
        repo.list_by_org_id = AsyncMock(return_value=[])

        async def fake_resolve(user_id: str) -> str:
            return f"{user_id}@example.com"

        monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
        monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
        monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

        r = TestClient(app).get(
            "/api/v1/teams/members",
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "members": [
                {
                    "id": "m1",
                    "principalId": "pcu_solo",
                    "email_via_clerk": "clerk_solo@example.com",
                }
            ]
        }
        # No GSI query for personal-context users.
        repo.list_by_org_id.assert_not_called()
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)
