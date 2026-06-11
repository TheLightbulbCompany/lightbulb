"""Default-payment-method promotion in the subscription webhook handlers.

Stripe Checkout attaches the collected payment method to the SUBSCRIPTION
only; the Customer's invoice_settings.default_payment_method stays empty.
Metronome's charge_automatically collection charges the CUSTOMER default,
so handle_subscription_state must promote the subscription's PM to the
customer default — idempotently, and without ever failing the webhook.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import stripe_webhook_handlers as handlers  # noqa: E402


def _subscription_event(**overrides) -> dict:
    event_data = {
        "id": "sub_x",
        "customer": "cus_x",
        "status": "trialing",
        "trial_end": 1700000000,
        "metadata": {"owner_id": "org_x"},
        "default_payment_method": "pm_x",
    }
    event_data.update(overrides)
    return {"id": "evt_x", "type": "customer.subscription.created", "data": {"object": event_data}}


@pytest.fixture
def mock_repo():
    with patch.object(handlers, "billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "org_x", "owner_type": "org", "stripe_customer_id": "cus_x"}
        )
        repo.set_subscription = AsyncMock()
        repo.set_provider_choice = AsyncMock()
        yield repo


@pytest.mark.asyncio
async def test_sets_customer_default_pm_when_empty(mock_repo):
    """Sub carries a PM + customer default is empty → promote it."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": None}}

        result = await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    assert result is None
    mock_stripe.Customer.retrieve.assert_called_once_with("cus_x")
    mock_stripe.Customer.modify.assert_called_once_with("cus_x", invoice_settings={"default_payment_method": "pm_x"})
    assert "billing.default_pm.set" in [c.args[0] for c in mock_metric.call_args_list]


@pytest.mark.asyncio
async def test_skips_when_customer_already_has_default(mock_repo):
    """An existing customer default is never clobbered (idempotent on
    .updated re-deliveries and on PM changes the user made deliberately)."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm_existing"}}

        await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    mock_stripe.Customer.modify.assert_not_called()
    metric_names = [c.args[0] for c in mock_metric.call_args_list]
    assert "billing.default_pm.set" not in metric_names
    assert "billing.default_pm.error" not in metric_names


@pytest.mark.asyncio
async def test_skips_when_subscription_carries_no_pm(mock_repo):
    """No default_payment_method on the subscription → no Stripe calls at
    all (the common .updated case for status-only transitions)."""
    with patch.object(handlers, "stripe") as mock_stripe:
        await handlers.handle_subscription_state(_subscription_event(default_payment_method=None), MagicMock())

    mock_stripe.Customer.retrieve.assert_not_called()
    mock_stripe.Customer.modify.assert_not_called()
    # The state sync itself still ran.
    mock_repo.set_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_expanded_pm_object_uses_its_id(mock_repo):
    """Events built with expansions carry the PM as an object; use its id."""
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {}}

        await handlers.handle_subscription_state(
            _subscription_event(default_payment_method={"id": "pm_expanded"}), MagicMock()
        )

    mock_stripe.Customer.modify.assert_called_once_with(
        "cus_x", invoice_settings={"default_payment_method": "pm_expanded"}
    )


@pytest.mark.asyncio
async def test_stripe_error_does_not_fail_handler(mock_repo):
    """Best-effort: a Stripe failure emits the error metric and the handler
    still completes the subscription-state sync (Stripe must get a 200, not
    a retry storm)."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.side_effect = Exception("stripe down")

        result = await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    assert result is None
    assert "billing.default_pm.error" in [c.args[0] for c in mock_metric.call_args_list]
    mock_repo.set_subscription.assert_awaited_once()
