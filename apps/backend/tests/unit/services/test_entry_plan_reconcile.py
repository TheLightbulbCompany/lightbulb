"""Entry tier transitions use current Stripe state and preserve the $50 provider choice."""

from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from core.services import stripe_webhook_handlers as handlers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_plan,provider,current_price,expected_provider",
    [
        ("standard", "byo_key", "price_entry", "opencode_zen"),
        ("entry", "opencode_zen", "price_workspace", "opencode_zen"),
        ("standard", "byo_key", "price_workspace", "byo_key"),
    ],
)
async def test_reconcile_uses_current_subscription_instead_of_old_event(
    monkeypatch, stored_plan, provider, current_price, expected_provider
):
    monkeypatch.setattr(handlers.settings, "STRIPE_ENTRY_PRICE_ID", "price_entry")
    monkeypatch.setattr(handlers.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    snapshot = {
        "owner_id": "org_a",
        "stripe_customer_id": "cus_a",
        "plan": stored_plan,
        "provider_choice": provider,
        "updated_at": "before",
    }
    monkeypatch.setattr(handlers.billing_repo, "get_by_owner_id", AsyncMock(return_value=snapshot))
    current = {
        "id": "sub_a",
        "customer": "cus_a",
        "items": {"data": [{"price": {"id": current_price, "recurring": {"interval": "month"}}}]},
    }
    monkeypatch.setattr(handlers.stripe.Subscription, "retrieve", MagicMock(return_value=current))
    write_provider = create_autospec(handlers.billing_repo.set_provider_choice)
    write_plan = AsyncMock()
    monkeypatch.setattr(handlers.billing_repo, "set_provider_choice", write_provider)
    monkeypatch.setattr(handlers.billing_repo, "set_plan", write_plan)
    actual, account = await handlers._reconcile_entry_plan(snapshot, {"id": "sub_a"})
    assert actual is current
    assert account["provider_choice"] == expected_provider
    if current_price == "price_entry" and provider != "opencode_zen":
        write_provider.assert_awaited_once_with(
            "org_a", provider_choice="opencode_zen", byo_provider=None, plan="entry", expected_updated_at="before"
        )
        write_plan.assert_not_awaited()
    elif stored_plan == "entry":
        assert write_plan.await_args.kwargs["plan"] == "standard"
        assert write_plan.await_args.kwargs["expected_updated_at"] == "before"
        write_provider.assert_not_awaited()
    else:
        write_provider.assert_not_awaited()
        write_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_refetches_stripe_after_a_concurrent_plan_write(monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.setattr(handlers.settings, "STRIPE_ENTRY_PRICE_ID", "price_entry")
    monkeypatch.setattr(handlers.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    account = {
        "owner_id": "org_a",
        "stripe_customer_id": "cus_a",
        "plan": "standard",
        "provider_choice": "byo_key",
        "updated_at": "old",
    }
    latest = {**account, "updated_at": "new"}
    monkeypatch.setattr(handlers.billing_repo, "get_by_owner_id", AsyncMock(side_effect=[account, latest]))

    def subscription(price):
        return {
            "id": "sub_a",
            "customer": "cus_a",
            "items": {"data": [{"price": {"id": price, "recurring": {"interval": "month"}}}]},
        }

    retrieve = MagicMock(side_effect=[subscription("price_entry"), subscription("price_workspace")])
    monkeypatch.setattr(handlers.stripe.Subscription, "retrieve", retrieve)
    write = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    )
    monkeypatch.setattr(handlers.billing_repo, "set_provider_choice", write)
    event, after = await handlers._reconcile_entry_plan(account, {"id": "sub_a"})
    assert event["items"]["data"][0]["price"]["id"] == "price_workspace"
    assert after["provider_choice"] == "byo_key"
    assert retrieve.call_count == 2
    assert write.await_count == 1


@pytest.mark.asyncio
async def test_reconciliation_rejects_another_customers_subscription(monkeypatch):
    account = {"owner_id": "org_a", "stripe_customer_id": "cus_a"}
    monkeypatch.setattr(handlers.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    monkeypatch.setattr(
        handlers.stripe.Subscription, "retrieve", MagicMock(return_value={"id": "sub_a", "customer": "cus_other"})
    )
    with pytest.raises(ValueError, match="another billing customer"):
        await handlers._reconcile_entry_plan(account, {"id": "sub_a"})
