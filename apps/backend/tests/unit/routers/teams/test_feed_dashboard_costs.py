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
        return AuthContext(user_id="u1", org_id="o1", org_role="org:admin")

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
        feed_mod,
        "resolve_provider_choice",
        AsyncMock(return_value=("bedrock_claude", None)),
    )
    monkeypatch.setattr(
        feed_mod.spend_service,
        "get_billed_month_spend",
        AsyncMock(return_value=BilledSpend(total_cents=1234, by_model=[], period_start=None)),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["dashboard"]["costs"] == {"monthSpendCents": 1234}


def test_dashboard_costs_present_for_legacy_owner_with_no_stored_choice(client, monkeypatch):
    """A legacy owner (billing row with no provider_choice, subscription
    active) must still get a costs block — resolve_provider_choice defaults
    the missing field to bedrock_claude, and the dashboard gate must key off
    the resolved value, not a raw ``account.get("provider_choice")`` read."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    from core.services import container_provision as container_provision_mod

    monkeypatch.setattr(
        container_provision_mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )
    monkeypatch.setattr(
        feed_mod.spend_service,
        "get_billed_month_spend",
        AsyncMock(return_value=BilledSpend(total_cents=4321, by_model=[], period_start=None)),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["dashboard"]["costs"] == {"monthSpendCents": 4321}


def test_dashboard_costs_omitted_for_trial_local_owner(client, monkeypatch):
    """A no-card trial_local owner resolves to the bedrock_trial config mode
    — the costs block must be omitted, matching /billing/spend's
    tokens_only. The free tier deliberately shows no dollars."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    from core.services import container_provision as container_provision_mod

    monkeypatch.setattr(
        container_provision_mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "trial_local"}),
    )
    get_billed = AsyncMock(return_value=BilledSpend(total_cents=0, by_model=[], period_start=None))
    monkeypatch.setattr(feed_mod.spend_service, "get_billed_month_spend", get_billed)

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]
    get_billed.assert_not_awaited()


def test_dashboard_costs_omitted_when_not_billed(client, monkeypatch):
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        feed_mod,
        "resolve_provider_choice",
        AsyncMock(return_value=("bedrock_claude", None)),
    )
    monkeypatch.setattr(
        feed_mod.spend_service,
        "get_billed_month_spend",
        AsyncMock(return_value=None),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]


def test_dashboard_costs_omitted_for_non_bedrock_owner_with_metronome_customer(client, monkeypatch):
    """Every provision-eligible owner gets a metronome_customer_id regardless
    of provider (see user_provision.py / metronome_provisioning.py) — so the
    costs block must gate on the resolved provider_choice, not on Metronome
    customer existence, or an OAuth/BYOK/trial owner gets a permanent
    misleading $0.00 Month Spend card."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        feed_mod,
        "resolve_provider_choice",
        AsyncMock(return_value=("chatgpt_oauth", None)),
    )
    get_billed = AsyncMock(return_value=BilledSpend(total_cents=0, by_model=[], period_start=None))
    monkeypatch.setattr(feed_mod.spend_service, "get_billed_month_spend", get_billed)

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]
    get_billed.assert_not_awaited()


def test_dashboard_costs_omitted_on_provider_resolution_error(client, monkeypatch):
    """A resolve_provider_choice failure (billing-account read error under
    the hood) must never 500 the dashboard — the costs block is
    display-only and degrades to omitted."""
    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        feed_mod,
        "resolve_provider_choice",
        AsyncMock(side_effect=RuntimeError("DDB throttled")),
    )

    r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]


def test_dashboard_costs_omitted_for_non_admin_org_member(client, monkeypatch):
    """GET /billing/spend restricts billed spend to org admins
    (require_org_admin) — a non-admin org member must not be able to read
    via the dashboard what the billing API 403s. The dashboard itself still
    returns 200 (it is not a billing-detail route), just without the costs
    block, even for a bedrock_claude owner."""
    from core.auth import AuthContext, get_current_user
    from routers.teams import agents as agents_mod
    from routers.teams import feed as feed_mod
    from main import app

    admin = MagicMock()
    admin.get_dashboard = AsyncMock(return_value=dict(PAPERCLIP_DASH))
    admin.get_sidebar_badges = AsyncMock(return_value={})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    resolve_mock = AsyncMock(return_value=("bedrock_claude", None))
    monkeypatch.setattr(feed_mod, "resolve_provider_choice", resolve_mock)
    get_billed = AsyncMock(return_value=BilledSpend(total_cents=999, by_model=[], period_start=None))
    monkeypatch.setattr(feed_mod.spend_service, "get_billed_month_spend", get_billed)

    async def fake_non_admin_auth():
        return AuthContext(user_id="member_1", org_id="o1", org_role="org:member")

    app.dependency_overrides[get_current_user] = fake_non_admin_auth
    try:
        r = client.get("/api/v1/teams/dashboard", headers={"Authorization": "Bearer x"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert r.status_code == 200
    assert "costs" not in r.json()["dashboard"]
    resolve_mock.assert_not_awaited()
    get_billed.assert_not_awaited()


def test_costs_route_deleted(client):
    r = client.get("/api/v1/teams/costs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404
