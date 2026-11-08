"""Workspace subscription item validation and plan derivation.

The plan no longer sizes anything. ``ultra_pro`` used to resize the owner's ECS task
(2 vCPU / 8 GB, and a reliability floor that refused to shrink a >10-agent roster back
down), so five of these tests were about ``resize_user_container``. An AgentCore runtime
has no per-owner size, so what remains is what the handler still does: derive and persist
the plan, and fail closed on a subscription shape it cannot read.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import stripe_webhook_handlers as handlers


@pytest.fixture(autouse=True)
def _stub_reconcile_dispatch(monkeypatch):
    """Live subscription events dispatch a durable reconcile (a Step Functions execution), and a
    dispatch that cannot start raises so Stripe redelivers. Stub it so no test reaches AWS."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    return dispatch


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
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch.object(handlers, "stripe") as mock_stripe,
    ):
        repo.get_by_owner_id = AsyncMock(return_value=account)
        repo.set_subscription = AsyncMock()
        repo.set_plan = AsyncMock(side_effect=set_plan_side_effect)
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())
        return repo


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
    repo = await _drive(_event(price_id="price_ultra"), account=_account())

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"


@pytest.mark.asyncio
async def test_a_downgrade_persists_the_standard_plan(monkeypatch):
    """The four "resize on downgrade" tests (shrink a small roster, refuse to shrink a large
    one, refuse on an unknown count, and never resize when set_plan itself failed) went with
    the ECS task size. What the downgrade still owes is the durable plan write."""
    repo = await _drive(_event(), account=_account(plan="ultra_pro"))

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"


@pytest.mark.asyncio
async def test_a_same_plan_rewrite_is_still_persisted():
    repo = await _drive(_event(price_id="price_ultra"), account=_account(plan="ultra_pro"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"


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


@pytest.mark.asyncio
async def test_a_plan_change_relimits_the_owner_openrouter_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.OPENROUTER_WORKSPACE_ID", "workspace-uuid")
    relimit = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.openrouter_keys.relimit_owner_key", relimit)
    await _drive(_event(), account=_account())
    relimit.assert_awaited_once_with("org_workspace")


@pytest.mark.asyncio
async def test_a_relimit_failure_never_raises_into_the_webhook(monkeypatch):
    monkeypatch.setattr("core.config.settings.OPENROUTER_WORKSPACE_ID", "workspace-uuid")
    monkeypatch.setattr("core.services.openrouter_keys.relimit_owner_key", AsyncMock(side_effect=RuntimeError("boom")))
    repo = await _drive(_event(), account=_account())
    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
