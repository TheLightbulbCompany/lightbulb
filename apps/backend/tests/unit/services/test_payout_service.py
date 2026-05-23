"""Tests for payout_service Stripe Connect Express scaffold."""

import os

# Match codebase pattern: seed CLERK_ISSUER before any core.* import.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import payout_service  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.payout_service.stripe")
async def test_create_connect_account_for_seller(mock_stripe):
    mock_stripe.Account.create.return_value = MagicMock(id="acct_test_123")
    result = await payout_service.create_connect_account(
        seller_id="user_abc",
        email="seller@example.com",
        country="US",
    )
    assert result == "acct_test_123"
    mock_stripe.Account.create.assert_called_once()
    call_kwargs = mock_stripe.Account.create.call_args.kwargs
    assert call_kwargs["type"] == "express"
    assert call_kwargs["country"] == "US"
    assert call_kwargs["email"] == "seller@example.com"
    assert call_kwargs["metadata"]["seller_id"] == "user_abc"
    # Idempotency key is fresh-uuid-per-call; must NOT be keyed on the stable
    # seller_id (Stripe's 24h cache would return a dead account_id after any
    # out-of-band Stripe-side delete).
    key = call_kwargs["idempotency_key"]
    assert "user_abc" not in key
    assert key.startswith("connect_account_create:")


@pytest.mark.asyncio
@patch("core.services.payout_service.stripe")
async def test_create_onboarding_link(mock_stripe):
    mock_stripe.AccountLink.create.return_value = MagicMock(url="https://connect.stripe.com/setup/abc123")
    url = await payout_service.create_onboarding_link(
        connect_account_id="acct_test_123",
        refresh_url="https://example.com/refresh",
        return_url="https://example.com/return",
    )
    assert url == "https://connect.stripe.com/setup/abc123"
    call_kwargs = mock_stripe.AccountLink.create.call_args.kwargs
    assert call_kwargs["account"] == "acct_test_123"
    assert call_kwargs["type"] == "account_onboarding"


@pytest.mark.asyncio
@patch("core.services.payout_service.stripe")
async def test_rejects_non_us_country(mock_stripe):
    """Per design doc, v1 = US sellers only."""
    with pytest.raises(payout_service.UnsupportedCountryError):
        await payout_service.create_connect_account(
            seller_id="user_abc",
            email="seller@example.com",
            country="DE",
        )
    mock_stripe.Account.create.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.payout_service.stripe")
async def test_refund_with_completed_transfer_reverses_it(mock_stripe):
    mock_stripe.Refund.create.return_value = MagicMock(id="re_abc")
    mock_stripe.Transfer.list.return_value = MagicMock(
        data=[
            MagicMock(id="tr_xyz", amount=1700, currency="usd"),
        ]
    )
    mock_stripe.Transfer.create_reversal.return_value = MagicMock(id="trr_pqr")

    result = await payout_service.refund_purchase(
        payment_intent_id="pi_abc",
        transfer_group="purchase_p1",
        full_amount_cents=2000,
    )
    assert result.refund_id == "re_abc"
    assert result.reversal_id == "trr_pqr"
    mock_stripe.Refund.create.assert_called_once()
    mock_stripe.Transfer.create_reversal.assert_called_once()


@pytest.mark.asyncio
@patch("core.services.payout_service.stripe")
async def test_refund_without_transfer_skips_reversal(mock_stripe):
    mock_stripe.Refund.create.return_value = MagicMock(id="re_abc")
    mock_stripe.Transfer.list.return_value = MagicMock(data=[])

    result = await payout_service.refund_purchase(
        payment_intent_id="pi_abc",
        transfer_group="purchase_p1",
        full_amount_cents=2000,
    )
    assert result.refund_id == "re_abc"
    assert result.reversal_id is None
    mock_stripe.Transfer.create_reversal.assert_not_called()
