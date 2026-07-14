"""Unit tests for billing_service.delete_stripe_customer — Admin
Account-Wipe Task 2.

Mirrors the mocking style of tests/unit/services/test_billing_service.py:
@patch billing_repo + stripe as MagicMocks on the billing_service module.
"""

from unittest.mock import AsyncMock, patch

import pytest
import stripe

from core.services.billing_service import BillingServiceError, delete_stripe_customer


def _resource_missing(message: str = "No such customer") -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError(message, param=None, code="resource_missing")


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_no_op_when_billing_row_missing(mock_stripe, mock_repo):
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)

    result = await delete_stripe_customer("user_no_billing")

    assert result is False
    mock_stripe.Customer.delete.assert_not_called()
    mock_stripe.Subscription.delete.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_no_op_when_no_stripe_customer_id(mock_stripe, mock_repo):
    """trial_local billing rows carry no stripe_customer_id at all."""
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_trial", "plan": "trial_local"})

    result = await delete_stripe_customer("user_trial")

    assert result is False
    mock_stripe.Customer.delete.assert_not_called()
    mock_stripe.Subscription.delete.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_deletes_customer_only_when_no_subscription(mock_stripe, mock_repo):
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_x", "stripe_customer_id": "cus_x"})

    result = await delete_stripe_customer("user_x")

    assert result is True
    mock_stripe.Subscription.delete.assert_not_called()
    mock_stripe.Customer.delete.assert_called_once()
    args, kwargs = mock_stripe.Customer.delete.call_args
    assert args[0] == "cus_x"


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_cancels_subscription_then_deletes_customer(mock_stripe, mock_repo):
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_y",
            "stripe_customer_id": "cus_y",
            "stripe_subscription_id": "sub_y",
        }
    )

    result = await delete_stripe_customer("user_y")

    assert result is True
    mock_stripe.Subscription.delete.assert_called_once()
    sub_args, _ = mock_stripe.Subscription.delete.call_args
    assert sub_args[0] == "sub_y"
    mock_stripe.Customer.delete.assert_called_once()
    cus_args, _ = mock_stripe.Customer.delete.call_args
    assert cus_args[0] == "cus_y"


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_swallows_resource_missing_on_subscription_cancel_and_still_deletes_customer(mock_stripe, mock_repo):
    """A re-run after a partial wipe failure: the subscription is already
    gone but the customer delete must still proceed."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_z",
            "stripe_customer_id": "cus_z",
            "stripe_subscription_id": "sub_gone",
        }
    )
    mock_stripe.Subscription.delete.side_effect = _resource_missing("No such subscription")

    result = await delete_stripe_customer("user_z")

    assert result is True
    mock_stripe.Customer.delete.assert_called_once()


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_swallows_resource_missing_on_customer_delete(mock_stripe, mock_repo):
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_w", "stripe_customer_id": "cus_gone"})
    mock_stripe.Customer.delete.side_effect = _resource_missing("No such customer")

    result = await delete_stripe_customer("user_w")

    assert result is True


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_raises_billing_service_error_on_unexpected_stripe_failure(mock_stripe, mock_repo):
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_v", "stripe_customer_id": "cus_v"})
    mock_stripe.Customer.delete.side_effect = RuntimeError("network blip")

    with pytest.raises(BillingServiceError, match="stripe_wipe_delete_failed"):
        await delete_stripe_customer("user_v")
