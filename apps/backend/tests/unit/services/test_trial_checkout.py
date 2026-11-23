"""T05: card at setup, first charge on the workspace's own day 31."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import billing_service

NOW = 1_800_000_000  # a bucket start (divisible by 300)
DAY = 86_400


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"subscription_status": "trial_local", "trial_end": NOW + 20 * DAY}, NOW + 20 * DAY),
        ({"subscription_status": "trial_local"}, NOW + 30 * DAY),  # unarmed: the full month
        ({"subscription_status": "trial_local", "trial_end": NOW + 3600}, NOW + 49 * 3600),  # Stripe's 48 h floor
        ({"subscription_status": "trial_local", "trial_end": NOW - 1}, None),  # expired: bill now
        ({"subscription_status": "canceled", "trial_end": NOW + DAY}, None),
        ({"subscription_status": "active"}, None),
    ],
)
def test_checkout_trial_ends_on_the_workspace_trial_end(row, expected):
    assert billing_service._checkout_trial_end(row, NOW) == expected


def test_price_lock_is_two_years():
    assert billing_service.price_lock_until(NOW) == "2029-01-14"


@pytest.mark.asyncio
async def test_marking_a_live_subscription_founding_stamps_the_lock_once(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_subscription_id": "sub_1"}),
    )
    monkeypatch.setattr(billing_service.billing_repo, "set_founding_team", AsyncMock())
    monkeypatch.setattr(billing_service.time, "time", lambda: NOW)
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", MagicMock(return_value={"metadata": {}}))
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    result = await billing_service.mark_founding_team("org_1")

    assert result["price_lock_until"] == "2029-01-14"
    modify.assert_called_once_with("sub_1", metadata={"price_lock_until": "2029-01-14"})

    modify.reset_mock()
    billing_service.stripe.Subscription.retrieve.return_value = {"metadata": {"price_lock_until": "2028-12-01"}}
    assert (await billing_service.mark_founding_team("org_1"))["price_lock_until"] == "2028-12-01"
    modify.assert_not_called()


def test_billing_has_no_seat_item():
    """One flat plan: no seat price, no seat count, no seat fields on the account."""
    from core.config import settings
    from schemas.billing import BillingAccountResponse

    assert not hasattr(settings, "STRIPE_SEAT_PRICE_ID") and not hasattr(settings, "SEAT_PRICE_USD")
    assert not hasattr(billing_service, "seats_for_workspace") and not hasattr(billing_service, "sync_seat_quantity")
    assert not {"seat_price_usd", "included_members"} & set(BillingAccountResponse.model_fields)


def test_the_retired_workspace_prices_and_checkout_are_gone():
    from core.config import settings
    from schemas.billing import BillingAccountResponse

    for name in (
        "STRIPE_WORKSPACE_PRICE_ID",
        "STRIPE_BASE_PRICE_ID",
        "STRIPE_ULTRA_PRICE_ID",
        "STRIPE_FRONT_DESK_PRICE_ID",
        "WORKSPACE_PRICE_USD",
        "BASE_PRICE_USD",
        "ULTRA_PRICE_USD",
        "FRONT_DESK_PRICE_USD",
    ):
        assert not hasattr(settings, name), name
    assert not hasattr(billing_service, "create_workspace_checkout")
    assert not {"workspace_price_usd", "base_price_usd", "ultra_price_usd", "front_desk", "front_desk_price_usd"} & set(
        BillingAccountResponse.model_fields
    )
