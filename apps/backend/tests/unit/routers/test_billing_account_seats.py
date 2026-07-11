"""GET /billing/account exposes seat data for org owners."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import billing  # noqa: E402


@pytest.mark.asyncio
async def test_account_includes_seats_for_org_owner(monkeypatch):
    monkeypatch.setattr(billing.settings, "TEAM_SEAT_PRICE_USD", 20)
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "active", "seat_count": 3}),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=5))

    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    assert resp.seat_count == 5
    assert resp.team_seat_price_usd == 20


@pytest.mark.asyncio
async def test_account_omits_seats_for_personal_owner(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "user_p", "subscription_status": "active"}),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    resp = await billing.get_billing_account(auth=AuthContext(user_id="user_p"))
    assert resp.seat_count is None
    assert resp.team_seat_price_usd is None


@pytest.mark.asyncio
async def test_account_is_seat_subscription_true_when_item_id_present(monkeypatch):
    """A real per-seat sub records subscription_item_id via the Stripe webhook."""
    monkeypatch.setattr(billing.settings, "TEAM_SEAT_PRICE_USD", 20)
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "seat_count": 3,
                "subscription_item_id": "si_123",
            }
        ),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=3))

    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    assert resp.is_seat_subscription is True


@pytest.mark.asyncio
async def test_account_is_seat_subscription_false_for_flat_comped_org(monkeypatch):
    """A flat/comped/grandfathered org sub has no subscription_item_id — must NOT
    be treated as per-seat billing (the fabricated 'N x $20' bug this guards)."""
    monkeypatch.setattr(billing.settings, "TEAM_SEAT_PRICE_USD", 20)
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "seat_count": 0,
            }
        ),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=3))

    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    assert resp.is_seat_subscription is False


@pytest.mark.asyncio
async def test_account_is_seat_subscription_false_for_personal_owner(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(
            return_value={
                "owner_id": "user_p",
                "subscription_status": "active",
                "subscription_item_id": "si_should_be_ignored",
            }
        ),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    resp = await billing.get_billing_account(auth=AuthContext(user_id="user_p"))
    assert resp.is_seat_subscription is False
