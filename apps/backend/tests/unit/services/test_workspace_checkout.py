"""Workspace-only Stripe subscription checkout contract."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import billing_service
from core.services.billing_service import BillingService


@pytest.fixture(autouse=True)
def checkout_slot(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "try_claim_checkout_slot",
        AsyncMock(return_value="claim"),
    )
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([]))),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "price_id"),
    [("workspace", "price_workspace"), ("ultra", "price_ultra")],
)
async def test_workspace_checkout_has_one_recurring_item_and_manual_promotion_codes(monkeypatch, plan, price_id):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    # Stripe objects are mapping-shaped; don't let a MagicMock hide accidental
    # attribute-only response handling at this public boundary.
    session = {"id": "cs_1", "url": "https://checkout.test/cs_1"}
    create = MagicMock(return_value=session)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan=plan)

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    kwargs = create.call_args.kwargs
    assert kwargs["line_items"] == [{"price": price_id, "quantity": 1}]
    assert kwargs["allow_promotion_codes"] is True
    assert kwargs["payment_method_collection"] == "always"
    assert kwargs["metadata"] == {
        "workspace_id": "org_1",
        "owner_id": "org_1",
        "clerk_user_id": "user_1",
        "plan": plan,
        "purpose": "workspace_subscription_v2",
    }
    assert "automatic_tax" not in kwargs
    assert "customer_update" not in kwargs
    assert "discounts" not in kwargs


@pytest.mark.asyncio
async def test_workspace_checkout_swaps_an_active_subscription_item_in_place(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "stripe_subscription_id": "sub_1",
                "subscription_status": "active",
            }
        ),
    )
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "retrieve",
        MagicMock(
            return_value={
                "id": "sub_1",
                "customer": "cus_1",
                "metadata": {"workspace_id": "org_1", "owner_id": "org_1", "workspace_plan_revision": "0"},
                "status": "active",
                "items": {"data": [{"id": "si_1", "price": {"id": "price_workspace"}}]},
            }
        ),
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="ultra")

    assert result == {"updated": True}
    assert modify.call_args.args == ("sub_1",)
    assert modify.call_args.kwargs["items"] == [{"id": "si_1", "price": "price_ultra"}]
    assert modify.call_args.kwargs["proration_behavior"] == "create_prorations"
    assert modify.call_args.kwargs["metadata"] == {"workspace_plan_revision": "1"}
    assert modify.call_args.kwargs["idempotency_key"].startswith("workspace-plan-change:v2:")


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_a_legacy_multi_item_subscription(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "stripe_subscription_id": "sub_legacy_team",
            }
        ),
    )
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "retrieve",
        MagicMock(
            return_value={
                "id": "sub_legacy_team",
                "customer": "cus_1",
                "metadata": {"workspace_id": "org_1", "owner_id": "org_1"},
                "status": "active",
                "items": {
                    "data": [
                        {"id": "si_base", "price": {"id": "price_workspace"}},
                        {"id": "si_seat", "price": {"id": "price_seat"}},
                    ]
                },
            }
        ),
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    with pytest.raises(billing_service.BillingServiceError, match="can't be changed automatically"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="ultra")

    modify.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_does_not_create_a_second_subscription_while_webhook_is_in_flight(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    subscription = {
        "id": "sub_webhook",
        "customer": "cus_1",
        "status": "active",
        "metadata": {"workspace_id": "org_1", "owner_id": "org_1", "workspace_plan_revision": "0"},
        "items": {"data": [{"id": "si_1", "price": {"id": "price_workspace"}}]},
    }
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([subscription]))),
    )
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="ultra")

    assert result == {"updated": True}
    assert modify.call_args.args[0] == "sub_webhook"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_reuses_its_open_pending_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    pending = {
        "status": "open",
        "url": "https://checkout.test/pending",
        "metadata": {"purpose": "workspace_subscription_v2", "plan": "workspace", "workspace_id": "org_1"},
        "customer": "cus_1",
        "mode": "subscription",
    }
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "pending_checkout_session_id": "cs_pending",
            }
        ),
    )
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", MagicMock(return_value=pending))
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/pending"}
    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_claim_denied_never_builds_a_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value=None))
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    with pytest.raises(billing_service.BillingServiceError, match="checkout_in_progress"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_marker_failure_expires_unreturned_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session, "create", MagicMock(return_value={"id": "cs_1", "url": "url"})
    )
    expire = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", expire)
    monkeypatch.setattr(
        billing_service.billing_repo, "set_pending_checkout", AsyncMock(side_effect=RuntimeError("ddb"))
    )

    with pytest.raises(billing_service.BillingServiceError, match="checkout_unavailable"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    expire.assert_called_once_with("cs_1")
    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_workspace_checkout_build_failure_releases_claim(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session, "create", MagicMock(side_effect=RuntimeError("stripe down"))
    )

    with pytest.raises(RuntimeError, match="stripe down"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_workspace_customer_does_not_reuse_another_workspaces_email_customer(monkeypatch):
    service = BillingService()
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.billing_repo,
        "create_if_not_exists",
        AsyncMock(return_value={"owner_id": "org_2", "stripe_customer_id": "cus_org_2"}),
    )
    create = MagicMock(return_value=MagicMock(id="cus_org_2"))
    monkeypatch.setattr(billing_service.stripe.Customer, "create", create)
    list_customers = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "list", list_customers)

    account = await service.create_customer_for_owner(owner_id="org_2", owner_type="org", email="owner@example.com")

    list_customers.assert_not_called()
    create.assert_called_once_with(
        metadata={"workspace_id": "org_2", "owner_id": "org_2", "owner_type": "org"},
        idempotency_key="workspace-customer:v2:org_2",
    )
    assert account["stripe_customer_id"] == "cus_org_2"


@pytest.mark.asyncio
async def test_workspace_customer_backfill_race_keeps_the_existing_row_winner(monkeypatch):
    service = BillingService()
    existing = {"owner_id": "org_2", "subscription_status": "trial_local"}
    winner = {"owner_id": "org_2", "stripe_customer_id": "cus_winner"}
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(side_effect=[existing, winner]),
    )
    monkeypatch.setattr(
        billing_service.billing_repo,
        "set_stripe_customer_id",
        AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already claimed"}},
                "UpdateItem",
            )
        ),
    )
    monkeypatch.setattr(
        billing_service.stripe.Customer,
        "create",
        MagicMock(return_value=MagicMock(id="cus_loser")),
    )
    delete = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "delete", delete)

    account = await service.create_customer_for_owner(owner_id="org_2", owner_type="org", email="owner@example.com")

    assert account == winner
    delete.assert_called_once_with("cus_loser", idempotency_key="delete_customer:cus_loser")
