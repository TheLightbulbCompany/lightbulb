"""handle_checkout_session funds a Metronome commit when LEDGER_BACKEND=metronome."""

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
async def test_funds_metronome_when_backend_metronome(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")

    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())

    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "org_abc"
    assert kwargs["amount_microcents"] == 25_000_000  # 2500 cents * 10_000
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["stripe_session_id"] == "cs_test_123"


@pytest.mark.asyncio
async def test_does_not_fund_metronome_when_backend_ddb(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "ddb")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())
    mock_fund.assert_not_awaited()


@pytest.mark.asyncio
async def test_metronome_funding_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch(
            "core.billing.metronome_funding.fund_top_up",
            new=AsyncMock(side_effect=RuntimeError("metronome down")),
        ),
    ):
        # Best-effort: a Metronome outage must not 500 the webhook.
        result = await handle_checkout_session(_session_event(), BillingService())
    assert result is None


@pytest.mark.asyncio
async def test_unpaid_session_funds_nothing(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock()) as mock_topup,
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(payment_status="unpaid"), BillingService())
    mock_topup.assert_not_awaited()
    mock_fund.assert_not_awaited()
