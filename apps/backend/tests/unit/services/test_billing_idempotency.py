"""Confirm Stripe Customer.create is idempotent per workspace."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@patch("core.services.billing_service.billing_repo")
@patch("core.services.billing_service.stripe")
async def test_create_customer_uses_workspace_idempotency_key(mock_stripe, mock_repo):
    from core.services.billing_service import BillingService

    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_stripe.Customer.create.return_value = MagicMock(id="cus_test")
    mock_repo.create_if_not_exists = AsyncMock(
        return_value={
            "owner_id": "u_1",
            "stripe_customer_id": "cus_test",
            "plan_tier": "free",
        }
    )

    await BillingService().create_customer_for_owner(owner_id="u_1", email="x@y.com")

    _, kwargs = mock_stripe.Customer.create.call_args
    assert kwargs["idempotency_key"] == "workspace-customer:v2:u_1"
