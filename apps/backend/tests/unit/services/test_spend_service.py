"""spend_service — provider-aware spend summary building blocks."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import spend_service


DRAFT_INVOICE = {
    "id": "inv-1",
    "start_timestamp": "2026-07-01T00:00:00+00:00",
    "line_items": [
        # usage line items carry our `tier` pricing group; totals are cents
        {"name": "Opus usage", "total": 900.0, "pricing_group_values": {"tier": "opus"}},
        {"name": "Sonnet usage", "total": 334.5, "pricing_group_values": {"tier": "sonnet"}},
        # commit draw-down: negative, no tier — must be excluded
        {"name": "Prepaid commit", "total": -1234.5, "pricing_group_values": None},
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    spend_service._spend_cache.clear()
    yield
    spend_service._spend_cache.clear()


def _patch_lifecycle(enabled: bool):
    return patch.object(spend_service.settings, "METRONOME_LIFECYCLE_ENABLED", enabled)


@pytest.mark.asyncio
async def test_billed_spend_sums_tier_line_items_and_skips_credit_items():
    client = AsyncMock()
    client.list_customer_invoices.return_value = [DRAFT_INVOICE]
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")

    assert billed is not None
    assert billed.total_cents == 1234  # 900 + 334.5 rounded, credit item excluded
    assert billed.period_start == "2026-07-01T00:00:00+00:00"
    tiers = {row["tier"]: row for row in billed.by_model}
    assert tiers["opus"]["cents"] == 900
    assert tiers["sonnet"]["cents"] == 334  # round-half-even is fine; assert int
    assert tiers["opus"]["model"] == "Claude Opus"


@pytest.mark.asyncio
async def test_billed_spend_none_when_lifecycle_off():
    with _patch_lifecycle(False):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_without_metronome_customer():
    with (
        _patch_lifecycle(True),
        patch.object(spend_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value={})),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_on_metronome_error():
    client = AsyncMock()
    client.list_customer_invoices.side_effect = RuntimeError("metronome down")
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_zero_when_no_draft_invoice_yet():
    client = AsyncMock()
    client.list_customer_invoices.return_value = []
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")
    assert billed is not None
    assert billed.total_cents == 0
    assert billed.by_model == []


@pytest.mark.asyncio
async def test_billed_spend_cached_within_ttl():
    client = AsyncMock()
    client.list_customer_invoices.return_value = [DRAFT_INVOICE]
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        await spend_service.get_billed_month_spend("owner-1")
        await spend_service.get_billed_month_spend("owner-1")
    assert client.list_customer_invoices.await_count == 1
