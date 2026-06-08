"""handle_checkout_session funds a Metronome commit on a paid top-up.

Phase 3 cutover: Metronome is the SOLE ledger, so a paid top-up funds a
Metronome prepaid commit UNCONDITIONALLY (no LEDGER_BACKEND gate). The legacy
DDB ledger's top-up shadow-write was deleted.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.billing_service import BillingService
from core.services.stripe_webhook_handlers import handle_checkout_session


def _session_event(payment_status="paid", purpose="credit_top_up"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_status": payment_status,
                "amount_total": 2500,
                "metadata": {
                    "purpose": purpose,
                    "owner_id": "org_abc",
                    "amount_cents": "2500",
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_funds_metronome_on_paid_top_up(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund:
        await handle_checkout_session(_session_event(), BillingService())
    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "org_abc"
    assert kwargs["amount_microcents"] == 25_000_000  # 2500 cents * 10_000
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["stripe_session_id"] == "cs_test_123"


@pytest.mark.asyncio
async def test_metronome_funding_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with patch(
        "core.billing.metronome_funding.fund_top_up",
        new=AsyncMock(side_effect=RuntimeError("metronome down")),
    ):
        # Best-effort: a Metronome outage must not 500 the webhook back to Stripe.
        result = await handle_checkout_session(_session_event(), BillingService())
    assert result is None


@pytest.mark.asyncio
async def test_unpaid_session_funds_nothing(monkeypatch):
    with patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund:
        await handle_checkout_session(_session_event(payment_status="unpaid"), BillingService())
    mock_fund.assert_not_awaited()
