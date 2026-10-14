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


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="claim"))
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())
    monkeypatch.setattr(billing_service, "seats_for_workspace", AsyncMock(return_value=0))
    monkeypatch.setattr(billing_service.time, "time", lambda: NOW + 10)
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([]))),
    )
    create = MagicMock(return_value={"id": "cs_1", "url": "https://checkout.test/cs_1"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    return create


@pytest.mark.asyncio
async def test_founding_trial_checkout_collects_a_card_and_charges_on_day_31(monkeypatch, checkout):
    row = {
        "owner_id": "org_1",
        "stripe_customer_id": "cus_1",
        "subscription_status": "trial_local",
        "trial_end": NOW + 29 * DAY,
        "founding_team": True,
    }
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))

    await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    kwargs = checkout.call_args.kwargs
    assert kwargs["payment_method_collection"] == "always"
    assert kwargs["allow_promotion_codes"] is False
    assert kwargs["subscription_data"]["trial_end"] == NOW + 29 * DAY
    assert kwargs["subscription_data"]["metadata"]["price_lock_until"] == "2029-01-14"


@pytest.mark.asyncio
async def test_non_founding_paid_checkout_has_no_trial_and_no_lock(monkeypatch, checkout):
    row = {"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "canceled"}
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))

    await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    kwargs = checkout.call_args.kwargs
    assert "trial_end" not in kwargs["subscription_data"]
    assert "price_lock_until" not in kwargs["metadata"]


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
