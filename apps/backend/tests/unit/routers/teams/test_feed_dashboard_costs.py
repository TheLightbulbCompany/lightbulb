"""/teams/dashboard costs block — Metronome-billed, never Paperclip cost_events."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services.spend_service import BilledSpend
from main import app
from routers.teams.deps import TeamsContext

PAPERCLIP_DASH = {
    "agents": {},
    "tasks": {},
    "costs": {"monthSpendCents": 55555, "monthBudgetCents": 0, "monthUtilizationPercent": 0},
}


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
def client(teams_ctx, monkeypatch):
    from core.auth import AuthContext, get_current_user
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    async def fake_auth():
        return AuthContext(user_id="o1")

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    app.dependency_overrides[get_current_user] = fake_auth

    # #802 container-readiness gate — default to reachable so the dashboard
    # returns 200 instead of 202 (mirrors the setup in test_feed.py).
    async def _reachable(owner_id: str):
        return True, "10.0.0.1", "gwtok"

    monkeypatch.setattr(agents_mod, "_container_is_reachable", _reachable)

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_dashboard_costs_replaced_with_billed_spend(client, monkeypatch):
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        feed_mod.spend_service,
        "get_billed_month_spend",
        AsyncMock(return_value=BilledSpend(total_cents=1234, by_model=[], period_start=None)),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["dashboard"]["costs"] == {"monthSpendCents": 1234}


def test_dashboard_costs_omitted_when_not_billed(client, monkeypatch):
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        feed_mod.spend_service,
        "get_billed_month_spend",
        AsyncMock(return_value=None),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]


def test_costs_route_deleted(client):
    r = client.get("/api/v1/teams/costs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404
