import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_stripe as mod  # noqa: E402


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin", org_id="org_a", org_role="org:admin", email="founder@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_member", org_id="org_a", org_role="org:member", email="m@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


def test_status_unbound(client):
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "account_id": None,
        "source": None,
        "details_submitted": False,
        "charges_enabled": False,
        "payouts_enabled": False,
        "dashboard_url": None,
    }


def test_create_binds_and_returns_link(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_new")) as create,
        patch.object(
            mod.billing_repo,
            "set_company_stripe_account",
            AsyncMock(return_value={"company_stripe_account_id": "acct_new"}),
        ) as bind,
        patch.object(
            mod.company_stripe,
            "create_company_onboarding_link",
            AsyncMock(return_value="https://connect.stripe.com/setup/x"),
        ),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "acct_new", "onboarding_url": "https://connect.stripe.com/setup/x"}
    create.assert_awaited_once_with(owner_id="org_a", email="founder@example.com", company_name="Northwind Ceramics")
    bind.assert_awaited_once_with("org_a", account_id="acct_new", source="created")


def test_create_reuses_existing_created_account(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_old", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock()) as create,
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock(return_value="https://x")),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acct_old"
    create.assert_not_awaited()


def test_create_refuses_when_connected_account_bound(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_theirs", "company_stripe_source": "connected"}
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 409


def test_create_requires_org_admin(member_client):
    resp = member_client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 403


def test_create_requires_paid(client, monkeypatch):
    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(
            side_effect=HTTPException(
                status_code=402, detail={"blocked": True, "code": "routines_require_subscription"}
            )
        ),
    )
    resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 402


def test_connect_start_stores_state(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.oauth_state_repo, "put", AsyncMock()) as put,
        patch.object(
            mod.company_stripe,
            "build_connect_authorize_url",
            return_value="https://connect.stripe.com/oauth/authorize?x",
        ),
    ):
        resp = client.get("/api/v1/company/stripe/connect/start")
    assert resp.status_code == 200
    assert resp.json() == {"authorize_url": "https://connect.stripe.com/oauth/authorize?x"}
    kwargs = put.call_args.kwargs
    assert kwargs["owner_id"] == "org_a"
    assert kwargs["provider"] == "stripe_company"


def test_connect_callback_binds_and_redirects(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value={})) as bind,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://dev.example/settings/connections?stripe=return&stripe_connect=connected"
    bind.assert_awaited_once_with("org_a", account_id="acct_theirs", source="connected")


def test_connect_callback_bad_state_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with patch.object(mod.oauth_state_repo, "consume", AsyncMock(return_value=None)):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=bad", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=state")


def test_disconnect_deauthorizes_and_clears(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
    deauth.assert_awaited_once_with(account_id="acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
