"""T04: seat billing from the 6th member."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import billing_service, stripe_webhook_handlers
from core.services.billing_service import BillingServiceError, sync_seat_quantity

LIVE_ROW = {
    "owner_id": "org_1",
    "stripe_customer_id": "cus_1",
    "stripe_subscription_id": "sub_1",
    "stripe_subscription_status": "active",
    "subscription_status": "active",
    "subscription_source": "stripe",
}


@pytest.fixture
def stripe_calls(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_SEAT_PRICE_ID", "price_seat")
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    calls = MagicMock()
    monkeypatch.setattr(billing_service.stripe.SubscriptionItem, "create", calls.create)
    monkeypatch.setattr(billing_service.stripe.SubscriptionItem, "modify", calls.modify)
    return calls


def _members(monkeypatch, n):
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=[{}] * n))


def _subscription(monkeypatch, items, status="active"):
    sub = {"id": "sub_1", "status": status, "items": {"data": items}}
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", MagicMock(return_value=sub))


def _row(monkeypatch, row):
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))


@pytest.mark.asyncio
@pytest.mark.parametrize(("members", "seats"), [(1, 0), (5, 0), (6, 1), (12, 7)])
async def test_seats_are_members_past_five(monkeypatch, members, seats):
    _members(monkeypatch, members)
    assert await billing_service.seats_for_workspace("org_1") == seats


@pytest.mark.asyncio
async def test_first_seat_adds_a_prorated_item(monkeypatch, stripe_calls):
    _row(monkeypatch, LIVE_ROW)
    _members(monkeypatch, 7)
    _subscription(monkeypatch, [{"id": "si_plan", "price": {"id": "price_workspace"}, "quantity": 1}])

    assert await sync_seat_quantity("org_1") == "changed"
    kwargs = stripe_calls.create.call_args.kwargs
    assert (kwargs["subscription"], kwargs["price"], kwargs["quantity"]) == ("sub_1", "price_seat", 2)
    assert kwargs["proration_behavior"] == "create_prorations"
    stripe_calls.modify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("members", "expected"), [(6, 1), (3, 0)])
async def test_member_change_sets_quantity_never_deletes(monkeypatch, stripe_calls, members, expected):
    _row(monkeypatch, LIVE_ROW)
    _members(monkeypatch, members)
    _subscription(
        monkeypatch,
        [
            {"id": "si_plan", "price": {"id": "price_workspace"}, "quantity": 1},
            {"id": "si_seat", "price": {"id": "price_seat"}, "quantity": 2},
        ],
    )

    assert await sync_seat_quantity("org_1") == "changed"
    stripe_calls.modify.assert_called_once_with("si_seat", quantity=expected, proration_behavior="create_prorations")
    stripe_calls.create.assert_not_called()


@pytest.mark.asyncio
async def test_unchanged_count_makes_no_stripe_write(monkeypatch, stripe_calls):
    _row(monkeypatch, LIVE_ROW)
    _members(monkeypatch, 4)
    _subscription(monkeypatch, [{"id": "si_plan", "price": {"id": "price_workspace"}, "quantity": 1}])

    assert await sync_seat_quantity("org_1") == "unchanged"
    stripe_calls.create.assert_not_called()
    stripe_calls.modify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {**LIVE_ROW, "subscription_source": "apple_iap"},
        {"owner_id": "org_1", "subscription_status": "trial_local"},
        {**LIVE_ROW, "stripe_subscription_status": "canceled"},
    ],
)
async def test_only_live_stripe_subscriptions_carry_seats(monkeypatch, stripe_calls, row):
    _row(monkeypatch, row)
    _members(monkeypatch, 9)

    assert await sync_seat_quantity("org_1") == "not_stripe_billed"
    stripe_calls.create.assert_not_called()
    stripe_calls.modify.assert_not_called()


@pytest.mark.asyncio
async def test_missing_seat_price_fails_closed(monkeypatch, stripe_calls):
    monkeypatch.setattr(billing_service.settings, "STRIPE_SEAT_PRICE_ID", "")
    _row(monkeypatch, LIVE_ROW)
    _members(monkeypatch, 9)
    retrieve = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", retrieve)

    assert await sync_seat_quantity("org_1") == "seat_price_unconfigured"
    retrieve.assert_not_called()
    stripe_calls.create.assert_not_called()


@pytest.mark.asyncio
async def test_add_front_desk_item_creates_once(monkeypatch, stripe_calls):
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    _row(monkeypatch, LIVE_ROW)
    plan = {"id": "si_plan", "price": {"id": "price_workspace"}, "quantity": 1}
    _subscription(monkeypatch, [plan])

    assert await billing_service.add_front_desk_item("org_1") == "added"
    kwargs = stripe_calls.create.call_args.kwargs
    assert kwargs == {
        "subscription": "sub_1",
        "price": "price_fd",
        "quantity": 1,
        "proration_behavior": "always_invoice",
        "idempotency_key": "front-desk-item:v1:sub_1",
    }

    _subscription(monkeypatch, [plan, {"id": "si_fd", "price": {"id": "price_fd"}, "quantity": 1}])
    assert await billing_service.add_front_desk_item("org_1") == "already"
    assert stripe_calls.create.call_count == 1


@pytest.mark.asyncio
async def test_add_front_desk_item_on_a_trial_ends_it_now_and_invoices(monkeypatch, stripe_calls):
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    _row(monkeypatch, LIVE_ROW)
    _subscription(
        monkeypatch, [{"id": "si_plan", "price": {"id": "price_workspace"}, "quantity": 1}], status="trialing"
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    assert await billing_service.add_front_desk_item("org_1") == "added"
    modify.assert_called_once_with(
        "sub_1",
        items=[{"price": "price_fd", "quantity": 1}],
        trial_end="now",
        proration_behavior="always_invoice",
        idempotency_key="front-desk-item:v1:sub_1",
    )
    stripe_calls.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["past_due", "incomplete", "unpaid", "paused"])
async def test_add_front_desk_item_only_on_active_or_trialing(monkeypatch, stripe_calls, status):
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    _row(monkeypatch, LIVE_ROW)
    _subscription(monkeypatch, [], status=status)
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    assert await billing_service.add_front_desk_item("org_1") == "not_live"
    stripe_calls.create.assert_not_called()
    modify.assert_not_called()


@pytest.mark.asyncio
async def test_add_front_desk_item_refusals(monkeypatch, stripe_calls):
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "")
    assert await billing_service.add_front_desk_item("org_1") == "front_desk_price_unconfigured"
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    _row(monkeypatch, LIVE_ROW)
    _subscription(monkeypatch, [], status="canceled")
    assert await billing_service.add_front_desk_item("org_1") == "not_live"
    stripe_calls.create.assert_not_called()


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="claim"))
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([]))),
    )
    _row(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    create = MagicMock(return_value={"id": "cs_1", "url": "https://checkout.test/cs_1"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    return create


@pytest.mark.asyncio
async def test_checkout_bills_seats_from_the_start(monkeypatch, stripe_calls, checkout):
    _members(monkeypatch, 8)
    await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")
    assert checkout.call_args.kwargs["line_items"] == [
        {"price": "price_workspace", "quantity": 1},
        {"price": "price_seat", "quantity": 3},
    ]


@pytest.mark.asyncio
async def test_checkout_refuses_six_members_without_a_seat_price(monkeypatch, stripe_calls, checkout):
    monkeypatch.setattr(billing_service.settings, "STRIPE_SEAT_PRICE_ID", "")
    _members(monkeypatch, 6)
    with pytest.raises(BillingServiceError, match="seat_price_unconfigured"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")
    checkout.assert_not_called()


def test_subscription_events_accept_the_seat_item(monkeypatch):
    monkeypatch.setattr(stripe_webhook_handlers.settings, "STRIPE_SEAT_PRICE_ID", "price_seat")
    monkeypatch.setattr(stripe_webhook_handlers.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    event = {
        "items": {
            "data": [
                {"price": {"id": "price_workspace", "recurring": {"interval": "month"}}},
                {"price": {"id": "price_seat", "recurring": {"interval": "month"}}},
            ]
        }
    }
    assert stripe_webhook_handlers._workspace_plan_from_subscription_items(event) == "standard"


def test_subscription_events_accept_the_front_desk_item(monkeypatch):
    monkeypatch.setattr(stripe_webhook_handlers.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(stripe_webhook_handlers.settings, "STRIPE_SEAT_PRICE_ID", "price_seat")
    monkeypatch.setattr(stripe_webhook_handlers.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    event = {
        "items": {
            "data": [
                {"price": {"id": "price_workspace", "recurring": {"interval": "month"}}},
                {"price": {"id": "price_seat", "recurring": {"interval": "month"}}},
                {"price": {"id": "price_fd", "recurring": {"interval": "month"}}},
            ]
        }
    }
    assert stripe_webhook_handlers._workspace_plan_from_subscription_items(event) == "standard"


@pytest.mark.asyncio
async def test_membership_seat_sync_failure_raises_for_redelivery(monkeypatch):
    from routers import webhooks

    monkeypatch.setattr(billing_service, "sync_seat_quantity", AsyncMock(side_effect=RuntimeError("stripe down")))
    with pytest.raises(RuntimeError):
        await webhooks._sync_seats("org_1")
