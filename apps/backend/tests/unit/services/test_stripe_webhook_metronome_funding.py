"""handle_checkout_session mirrors a paid top-up into Metronome whenever the
owner's billing row carries a metronome_customer_id (lifecycle exists ⇒ fund),
regardless of LEDGER_BACKEND — there is no balance backfill at cutover, so an
unmirrored top-up would be missing from the Metronome balance later."""

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


def _billing_row_mock(metronome_customer_id):
    row = {"owner_id": "org_abc"}
    if metronome_customer_id:
        row["metronome_customer_id"] = metronome_customer_id
        row["metronome_contract_id"] = "contract-1"
    return patch(
        "core.services.stripe_webhook_handlers.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=row),
    )


@pytest.mark.asyncio
async def test_funds_metronome_when_billing_row_has_customer_id(monkeypatch):
    # LEDGER_BACKEND stays "ddb" during the transition — funding must key off
    # the owner's provisioned lifecycle, not the ledger flag.
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "ddb")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")

    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        _billing_row_mock("cust-uuid-1"),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())

    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "org_abc"
    assert kwargs["amount_microcents"] == 25_000_000  # 2500 cents * 10_000
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["stripe_session_id"] == "cs_test_123"
    # The handler already read the billing row to decide whether to fund —
    # both ids are passed through so fund_top_up skips a second read.
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["metronome_contract_id"] == "contract-1"


@pytest.mark.asyncio
async def test_does_not_fund_when_no_metronome_customer(monkeypatch):
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        _billing_row_mock(None),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())
    mock_fund.assert_not_awaited()


@pytest.mark.asyncio
async def test_metronome_funding_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        _billing_row_mock("cust-uuid-1"),
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
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock()) as mock_topup,
        _billing_row_mock("cust-uuid-1"),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(payment_status="unpaid"), BillingService())
    mock_topup.assert_not_awaited()
    mock_fund.assert_not_awaited()
