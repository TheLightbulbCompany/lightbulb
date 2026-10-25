"""Teams BFF — Paperclip budgets/costs proxy. Reads: any member. PATCHes: org admin only."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from main import app
from routers.teams import agents as agents_mod
from routers.teams.deps import TeamsContext

H = {"Authorization": "Bearer x"}


@pytest.fixture
def admin(monkeypatch):
    m = MagicMock()
    m.get_budgets_overview = AsyncMock(return_value={"policies": []})
    m.get_costs_summary = AsyncMock(return_value={"spendCents": 5})
    m.update_company_budget = AsyncMock(return_value={"id": "co_abc"})
    m.update_agent_budget = AsyncMock(return_value={"id": "ag_1"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: m)
    return m


def _client(role):
    ctx = TeamsContext(
        user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc", paperclip_user_id="p", session_cookie="cookie"
    )

    async def fake_ctx():
        return ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="u1", org_id="o1", org_role=role)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.pop(agents_mod._ctx, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_member_can_read_overview_and_summary(admin):
    c = _client("org:member")
    assert c.get("/api/v1/teams/budgets/overview", headers=H).status_code == 200
    admin.get_budgets_overview.assert_awaited_once_with(company_id="co_abc", session_cookie="cookie")
    r = c.get("/api/v1/teams/costs/summary?from=2026-10-01T00:00:00Z", headers=H)
    assert r.status_code == 200
    admin.get_costs_summary.assert_awaited_once_with(
        company_id="co_abc", session_cookie="cookie", params={"from": "2026-10-01T00:00:00Z"}
    )


def test_admin_sets_company_and_agent_budget(admin):
    c = _client("org:admin")
    assert c.patch("/api/v1/teams/budgets", json={"budgetMonthlyCents": 5000}, headers=H).status_code == 200
    admin.update_company_budget.assert_awaited_once_with(
        company_id="co_abc", budget_monthly_cents=5000, session_cookie="cookie"
    )
    r = c.patch("/api/v1/teams/agents/ag_1/budget", json={"budgetMonthlyCents": 0}, headers=H)
    assert r.status_code == 200
    admin.update_agent_budget.assert_awaited_once_with(agent_id="ag_1", budget_monthly_cents=0, session_cookie="cookie")


def test_member_cannot_set_budgets(admin):
    c = _client("org:member")
    assert c.patch("/api/v1/teams/budgets", json={"budgetMonthlyCents": 5000}, headers=H).status_code == 403
    assert c.patch("/api/v1/teams/agents/ag_1/budget", json={"budgetMonthlyCents": 5}, headers=H).status_code == 403
    admin.update_company_budget.assert_not_awaited()
    admin.update_agent_budget.assert_not_awaited()


def test_negative_budget_rejected(admin):
    c = _client("org:admin")
    assert c.patch("/api/v1/teams/budgets", json={"budgetMonthlyCents": -1}, headers=H).status_code == 422
