"""Workspace subscription item validation and plan derivation."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import stripe_webhook_handlers as handlers


@pytest.fixture(autouse=True)
def configured_prices(monkeypatch):
    monkeypatch.setattr("core.config.settings.STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr("core.config.settings.STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())


def _event(*, price_id="price_workspace", items=None):
    item = {"id": "si_1", "price": {"id": price_id, "recurring": {"interval": "month"}}}
    return {
        "id": "evt_subscription",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_workspace",
                "customer": "cus_workspace",
                "status": "active",
                "metadata": {"workspace_id": "org_workspace"},
                "items": {"data": [item] if items is None else items},
            }
        },
    }


def _account(plan="standard"):
    return {
        "owner_id": "org_workspace",
        "stripe_customer_id": "cus_workspace",
        "stripe_subscription_id": "sub_workspace",
        "subscription_status": "active",
        "plan": plan,
    }


async def _drive(event, *, account, agents=None, set_plan_side_effect=None):
    manager = MagicMock()
    manager.resize_user_container = AsyncMock()
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=list(agents or [])),
        ),
        patch.object(handlers, "stripe") as mock_stripe,
    ):
        repo.get_by_owner_id = AsyncMock(return_value=account)
        repo.set_subscription = AsyncMock()
        repo.set_plan = AsyncMock(side_effect=set_plan_side_effect)
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())
        return repo, manager


@pytest.mark.asyncio
async def test_workspace_item_persists_standard_plan_before_facts():
    with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
        repo.get_by_owner_id = AsyncMock(return_value=_account())
        repo.set_subscription = AsyncMock()
        repo.set_plan = AsyncMock()
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(_event(), MagicMock())

    repo.set_subscription.assert_awaited_once()
    assert repo.set_plan.await_args.kwargs["plan"] == "standard"


@pytest.mark.asyncio
async def test_ultra_item_persists_ultra_plan():
    repo, manager = await _drive(_event(price_id="price_ultra"), account=_account())

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    manager.resize_user_container.assert_awaited_once_with("org_workspace", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_workspace_downgrade_resizes_small_company_down():
    from core.containers.ecs_manager import PER_USER_CPU, PER_USER_MEMORY_MIB

    repo, manager = await _drive(
        _event(), account=_account(plan="ultra_pro"), agents=[{"id": str(i)} for i in range(5)]
    )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    manager.resize_user_container.assert_awaited_once_with(
        "org_workspace", new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB
    )


@pytest.mark.asyncio
async def test_workspace_downgrade_with_large_roster_resizes_without_count_lookup():
    count_agents = AsyncMock(return_value=37)
    with patch(
        "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
        count_agents,
    ):
        repo, manager = await _drive(
            _event(), account=_account(plan="ultra_pro"), agents=[{"id": str(i)} for i in range(11)]
        )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    manager.resize_user_container.assert_awaited_once_with("org_workspace", new_cpu="1024", new_memory="4096")
    count_agents.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_plan_failure_skips_ultra_resize():
    """Never resize when Stripe's plan cannot be durably reflected locally."""
    repo, manager = await _drive(
        _event(price_id="price_ultra"),
        account=_account(),
        set_plan_side_effect=RuntimeError("DynamoDB unavailable"),
    )

    repo.set_plan.assert_awaited_once()
    manager.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_plan_rewrite_does_not_resize():
    repo, manager = await _drive(_event(price_id="price_ultra"), account=_account(plan="ultra_pro"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    manager.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [],
        [
            {"id": "si_1", "price": {"id": "price_workspace", "recurring": {"interval": "month"}}},
            {"id": "si_2", "price": {"id": "price_addon", "recurring": {"interval": "month"}}},
        ],
        [{"id": "si_1", "price": {"id": "price_workspace"}}],
        [{"id": "si_1", "price": {"id": "price_unknown", "recurring": {"interval": "month"}}}],
    ],
)
async def test_invalid_subscription_shape_fails_closed_before_durable_writes(items):
    with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
        repo.get_by_owner_id = AsyncMock(return_value=_account())
        repo.set_subscription = AsyncMock()
        repo.set_plan = AsyncMock()
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(_event(items=items), MagicMock())

    repo.set_subscription.assert_not_awaited()
    repo.set_plan.assert_not_awaited()
