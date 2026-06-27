"""Tests for marketplace_payouts router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
import stripe  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def test_onboard_requires_auth(client):
    resp = client.post("/api/v1/marketplace/payouts/onboard")
    assert resp.status_code in (401, 403)


def test_dashboard_requires_auth(client):
    resp = client.get("/api/v1/marketplace/payouts/dashboard")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# require_org_admin gate on Connect onboarding + dashboard link
# ---------------------------------------------------------------------------


@pytest.fixture
def org_member_client():
    """TestClient authed as a non-admin org member.

    Stripe Connect onboarding + dashboard access bind to the org's payout
    account, so non-admins cannot reach either surface.
    """
    from main import app

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_test",
        org_id="org_test",
        org_role="org:member",
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("routers.marketplace_payouts.payout_service.create_connect_account")
@patch("routers.marketplace_payouts._payout_accounts_table")
def test_onboard_non_admin_org_member_403(mock_table, mock_create_acct, org_member_client):
    resp = org_member_client.post("/api/v1/marketplace/payouts/onboard")
    assert resp.status_code == 403
    mock_table.assert_not_called()
    mock_create_acct.assert_not_called()


@patch("routers.marketplace_payouts.stripe.Account.create_login_link")
@patch("routers.marketplace_payouts._payout_accounts_table")
def test_dashboard_non_admin_org_member_403(mock_table, mock_login_link, org_member_client):
    resp = org_member_client.get("/api/v1/marketplace/payouts/dashboard")
    assert resp.status_code == 403
    mock_table.assert_not_called()
    mock_login_link.assert_not_called()


# ---------------------------------------------------------------------------
# GET /dashboard — balance always renders; Stripe failure degrades gracefully
# ---------------------------------------------------------------------------


@pytest.fixture
def seller_client():
    """TestClient authed as a personal-context seller (passes require_org_admin)."""
    from main import app

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="seller_test")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("routers.marketplace_payouts.stripe.Account.create_login_link")
@patch("routers.marketplace_payouts._payout_accounts_table")
def test_dashboard_no_payout_row_returns_balance_fields(mock_table, mock_login_link, seller_client):
    """No payout row → dashboard_url None + both balance fields present (no Stripe call)."""
    mock_table.return_value.get_item.return_value = {}

    resp = seller_client.get("/api/v1/marketplace/payouts/dashboard")

    assert resp.status_code == 200
    assert resp.json() == {
        "dashboard_url": None,
        "balance_held_cents": 0,
        "lifetime_earned_cents": 0,
    }
    mock_login_link.assert_not_called()


@patch("routers.marketplace_payouts.stripe.Account.create_login_link")
@patch("routers.marketplace_payouts._payout_accounts_table")
def test_dashboard_stripe_error_degrades_gracefully(mock_table, mock_login_link, seller_client):
    """Connect account present but Stripe raises → null dashboard_url, NOT a 500."""
    mock_table.return_value.get_item.return_value = {
        "Item": {
            "stripe_connect_account_id": "acct_unfinished",
            "balance_held_cents": 1234,
            "lifetime_earned_cents": 5678,
        }
    }
    mock_login_link.side_effect = stripe.error.StripeError("account onboarding incomplete")

    resp = seller_client.get("/api/v1/marketplace/payouts/dashboard")

    assert resp.status_code == 200
    assert resp.json() == {
        "dashboard_url": None,
        "balance_held_cents": 1234,
        "lifetime_earned_cents": 5678,
    }
    mock_login_link.assert_called_once_with("acct_unfinished")


@patch("routers.marketplace_payouts.stripe.Account.create_login_link")
@patch("routers.marketplace_payouts._payout_accounts_table")
def test_dashboard_success_returns_login_link(mock_table, mock_login_link, seller_client):
    """Onboarded account → dashboard_url from Stripe + balance fields."""
    mock_table.return_value.get_item.return_value = {
        "Item": {
            "stripe_connect_account_id": "acct_ready",
            "balance_held_cents": 4200,
            "lifetime_earned_cents": 9000,
        }
    }
    mock_login_link.return_value = SimpleNamespace(url="https://connect.stripe.com/express/login123")

    resp = seller_client.get("/api/v1/marketplace/payouts/dashboard")

    assert resp.status_code == 200
    assert resp.json() == {
        "dashboard_url": "https://connect.stripe.com/express/login123",
        "balance_held_cents": 4200,
        "lifetime_earned_cents": 9000,
    }
    mock_login_link.assert_called_once_with("acct_ready")
