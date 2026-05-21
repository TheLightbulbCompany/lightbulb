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
    admin.list_activity.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


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
