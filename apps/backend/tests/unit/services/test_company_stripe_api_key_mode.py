"""A test-mode restricted key binds in every environment.

Stripe's App Marketplace reviewers may only use test Stripe accounts, and a
customer trying the Stripe App in a sandbox mints an rk_test_ key too. Both
must reach production, so the mode is reported (for the card's "Test mode"
label) instead of refused.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.services import company_stripe as mod


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "livemode"), [("rk_test_" + "x" * 24, False), ("rk_live_" + "x" * 24, True)])
async def test_validate_api_key_accepts_both_modes_and_reports_which(key, livemode):
    account = {"id": "acct_k", "details_submitted": True, "charges_enabled": True, "payouts_enabled": False}
    with (
        patch.object(mod.stripe.Account, "retrieve", MagicMock(return_value=account)) as retrieve,
        patch.object(mod, "_probe_read_permissions", MagicMock()),
    ):
        info = await mod.validate_api_key(key)
    retrieve.assert_called_once_with(api_key=key)
    assert info == {
        "account_id": "acct_k",
        "livemode": livemode,
        "details_submitted": True,
        "charges_enabled": True,
        "payouts_enabled": False,
    }


@pytest.mark.asyncio
async def test_retrieve_status_with_a_key_carries_the_mode():
    account = {"id": "acct_k", "details_submitted": True, "charges_enabled": False, "payouts_enabled": False}
    with patch.object(mod.stripe.Account, "retrieve", MagicMock(return_value=account)):
        status = await mod.retrieve_status(account_id="acct_k", api_key="rk_test_" + "y" * 24)
    assert status["livemode"] is False and status["charges_enabled"] is False
