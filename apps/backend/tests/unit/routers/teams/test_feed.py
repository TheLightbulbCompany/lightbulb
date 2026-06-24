"""Tests for the Teams Feed BFF — Activity + Costs + Dashboard."""

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


def test_list_activity(client, monkeypatch):
    admin = MagicMock()
    admin.list_activity = AsyncMock(return_value={"events": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/activity",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    # No client limit → forwarded as None (admin client omits the param).
    admin.list_activity.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        limit=None,
    )


def test_list_activity_forwards_limit(client, monkeypatch):
    """The Activity feed passes ?limit=200; the BFF forwards it so upstream
    returns more than its default 100."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(return_value={"events": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/activity",
        params={"limit": 200},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_activity.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
        limit=200,
    )


def test_list_activity_rejects_over_max_limit(client, monkeypatch):
    """``limit`` is bounded to upstream's MAX_ACTIVITY_LIMIT (500)."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(return_value={"events": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/activity",
        params={"limit": 999},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.list_activity.assert_not_awaited()


def test_list_activity_enriches_user_actor_email_and_name(client, monkeypatch):
    """User-actor events get ``actorEmail`` / ``actorName`` resolved via the
    principalId → Clerk user_id → email join (the same join members.py uses).
    Agent/system actors are untouched."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value={
            "events": [
                {"id": "e1", "actorType": "user", "actorId": "pcu_aaa", "action": "issue.created"},
                {"id": "e2", "actorType": "agent", "actorId": "agent_x", "action": "agent.updated"},
                {"id": "e3", "actorType": "system", "actorId": None, "action": "issue.deleted"},
            ]
        }
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    resolve_calls: list[str] = []

    async def fake_resolve(user_id: str) -> str:
        resolve_calls.append(user_id)
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get(
        "/api/v1/teams/activity",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    events = r.json()["events"]
    # User actor enriched.
    assert events[0]["actorEmail"] == "clerk_aaa@example.com"
    assert events[0]["actorName"] == "clerk_aaa@example.com"
    # Agent + system actors untouched (no email/name fields).
    assert "actorEmail" not in events[1]
    assert "actorName" not in events[1]
    assert "actorEmail" not in events[2]
    # Resolved with the CLERK id, never the Paperclip principal.
    assert resolve_calls == ["clerk_aaa"]
    repo.list_by_org_id.assert_awaited_once_with("o1")


def test_list_activity_resolves_each_unique_actor_once(client, monkeypatch):
    """Two events from the same actor only trigger a single Clerk lookup
    (the enrichment batches unique actor ids)."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value={
            "events": [
                {"id": "e1", "actorType": "user", "actorId": "pcu_aaa", "action": "issue.created"},
                {"id": "e2", "actorType": "user", "actorId": "pcu_aaa", "action": "issue.updated"},
            ]
        }
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    resolve_calls: list[str] = []

    async def fake_resolve(user_id: str) -> str:
        resolve_calls.append(user_id)
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    events = r.json()["events"]
    assert events[0]["actorEmail"] == "clerk_aaa@example.com"
    assert events[1]["actorEmail"] == "clerk_aaa@example.com"
    assert resolve_calls == ["clerk_aaa"]


def test_list_activity_leaves_id_when_actor_unmapped(client, monkeypatch):
    """An actorId not in the org map (drift) ships untouched — no exception,
    no email/name stamped."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value={
            "events": [
                {"id": "e1", "actorType": "user", "actorId": "pcu_orphan", "action": "issue.created"},
            ]
        }
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    resolve_calls: list[str] = []

    async def fake_resolve(user_id: str) -> str:
        resolve_calls.append(user_id)
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    events = r.json()["events"]
    assert "actorEmail" not in events[0]
    assert "actorName" not in events[0]
    assert events[0]["actorId"] == "pcu_orphan"
    # Never called Clerk for the unmapped principal.
    assert resolve_calls == []


def test_list_activity_tolerates_clerk_failure(client, monkeypatch):
    """A Clerk lookup failure leaves the raw id on the event and never
    crashes the feed."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value={
            "events": [
                {"id": "e1", "actorType": "user", "actorId": "pcu_aaa", "action": "issue.created"},
            ]
        }
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    async def fake_resolve(user_id: str) -> str:
        raise RuntimeError("clerk down")

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    events = r.json()["events"]
    assert "actorEmail" not in events[0]
    assert events[0]["actorId"] == "pcu_aaa"


def test_list_activity_no_user_actors_skips_org_lookup(client, monkeypatch):
    """When no event has a user actor, the GSI/Clerk join is skipped
    entirely (a feed of only agent/system events does no extra work)."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value={
            "events": [
                {"id": "e1", "actorType": "agent", "actorId": "agent_x", "action": "agent.updated"},
            ]
        }
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[])

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)

    r = client.get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    repo.list_by_org_id.assert_not_called()


def test_list_activity_enriches_bare_list_payload(client, monkeypatch):
    """A bare-list activity payload (no envelope) is also enriched in place."""
    admin = MagicMock()
    admin.list_activity = AsyncMock(
        return_value=[
            {"id": "e1", "actorType": "user", "actorId": "pcu_aaa", "action": "issue.created"},
        ]
    )
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"user_id": "clerk_aaa", "paperclip_user_id": "pcu_aaa"}])

    async def fake_resolve(user_id: str) -> str:
        return f"{user_id}@example.com"

    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

    r = client.get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body[0]["actorEmail"] == "clerk_aaa@example.com"


def test_list_activity_personal_context_uses_self_pairing(client, monkeypatch):
    """For a personal user (org_id None) the GSI is skipped and the caller's
    own paperclip_user_id → user_id pairing is used to resolve their email."""
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
        admin.list_activity = AsyncMock(
            return_value={
                "events": [
                    {"id": "e1", "actorType": "user", "actorId": "pcu_solo", "action": "issue.created"},
                ]
            }
        )
        repo = MagicMock()
        repo.list_by_org_id = AsyncMock(return_value=[])

        async def fake_resolve(user_id: str) -> str:
            return f"{user_id}@example.com"

        monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
        monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
        monkeypatch.setattr(agents_mod, "_resolve_user_email", fake_resolve)

        r = TestClient(app).get("/api/v1/teams/activity", headers={"Authorization": "Bearer x"})
        assert r.status_code == 200
        events = r.json()["events"]
        assert events[0]["actorEmail"] == "clerk_solo@example.com"
        # Personal context never queries the org GSI.
        repo.list_by_org_id.assert_not_called()
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_get_costs(client, monkeypatch):
    admin = MagicMock()
    admin.get_costs = AsyncMock(return_value={"total": 0})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/costs",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_costs.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_dashboard_returns_upstream_shape_verbatim(client, monkeypatch):
    """Dashboard endpoint must return the upstream Paperclip summary
    verbatim — agents / tasks / costs / runActivity stay nested so the
    panel can render full metric tiles + charts without further fan-out.
    sidebar_badges is passed through unchanged."""
    upstream_dashboard = {
        "companyId": "co_abc",
        "agents": {"active": 2, "running": 1, "paused": 4, "error": 1},
        "tasks": {"open": 5, "inProgress": 2, "blocked": 1, "done": 7},
        "costs": {
            "monthSpendCents": 12345,
            "monthBudgetCents": 50000,
            "monthUtilizationPercent": 24.69,
        },
        "runActivity": [
            {"date": "2026-04-19", "total": 3},
            {"date": "2026-05-02", "total": 8},
        ],
    }
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=upstream_dashboard)
    admin.get_sidebar_badges = AsyncMock(return_value={"inbox": 3})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/dashboard",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "dashboard": upstream_dashboard,
        "sidebar_badges": {"inbox": 3},
    }


def test_dashboard_normalizes_none_to_empty_dict(client, monkeypatch):
    """If upstream returns ``None`` (or any non-dict), the BFF must
    normalize to an empty dict so the frontend can still render an
    empty-state card stack instead of crashing on ``null``."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=None)
    admin.get_sidebar_badges = AsyncMock(return_value={"inbox": 0})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/dashboard",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "dashboard": {},
        "sidebar_badges": {"inbox": 0},
    }


def test_dashboard_aggregates_dashboard_and_badges_in_parallel(client, monkeypatch):
    """Both upstream calls must fire (the route uses ``asyncio.gather``)
    and each must receive the company_id + session_cookie from the
    Teams context."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value={"agents": {}, "tasks": {}})
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/dashboard",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_dashboard.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )
    admin.get_sidebar_badges.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_get_sidebar_badges(client, monkeypatch):
    admin = MagicMock()
    admin.get_sidebar_badges = AsyncMock(return_value={"inbox": 2})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/sidebar-badges",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_sidebar_badges.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )
