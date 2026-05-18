"""Tests for marketplace_payouts router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
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
        org_role="org:basic_member",
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
