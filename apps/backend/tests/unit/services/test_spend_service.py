"""spend_service — provider-aware spend summary building blocks."""

from unittest.mock import AsyncMock, patch

import pytest

from core.billing.metronome_client import MetronomeClient
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


@pytest.mark.asyncio
async def test_billed_spend_sums_tier_line_items_and_skips_credit_items():
    with (
        patch.object(spend_service, "_list_draft_invoices", new=AsyncMock(return_value=[DRAFT_INVOICE])),
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
async def test_billed_spend_none_without_metronome_customer():
    with (
        patch.object(spend_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value={})),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_on_metronome_error():
    with (
        patch.object(
            spend_service,
            "_list_draft_invoices",
            new=AsyncMock(side_effect=RuntimeError("metronome down")),
        ),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_zero_when_no_draft_invoice_yet():
    with (
        patch.object(spend_service, "_list_draft_invoices", new=AsyncMock(return_value=[])),
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
    invoices_mock = AsyncMock(return_value=[DRAFT_INVOICE])
    with (
        patch.object(spend_service, "_list_draft_invoices", new=invoices_mock),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        await spend_service.get_billed_month_spend("owner-1")
        await spend_service.get_billed_month_spend("owner-1")
    assert invoices_mock.await_count == 1


@pytest.mark.asyncio
async def test_billed_spend_uses_invoice_detail_when_list_omits_line_items():
    """Finding P1: Metronome's list endpoint returns invoice SUMMARIES whose
    line_items may be empty ("use the Get Invoice endpoint for detailed
    line items" per Metronome's docs) — the real ``_list_draft_invoices``
    (not mocked away here) must re-fetch each invoice by id and sum the
    DETAIL response, not the list one."""
    list_invoice = {"id": "inv-1", "start_timestamp": "2026-07-01T00:00:00+00:00", "line_items": []}
    detail_invoice = {
        "id": "inv-1",
        "start_timestamp": "2026-07-01T00:00:00+00:00",
        "line_items": [
            {"name": "Sonnet usage", "total": 500.0, "pricing_group_values": {"tier": "sonnet"}},
        ],
    }
    with (
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
        patch.object(MetronomeClient, "list_customer_invoices", new=AsyncMock(return_value=[list_invoice])),
        patch.object(
            MetronomeClient, "get_customer_invoice", new=AsyncMock(return_value=detail_invoice)
        ) as get_invoice,
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")

    get_invoice.assert_awaited_once_with(customer_id="cus-1", invoice_id="inv-1")
    assert billed is not None
    assert billed.total_cents == 500
    assert billed.by_model == [{"model": "Claude Sonnet", "tier": "sonnet", "cents": 500}]


@pytest.mark.asyncio
async def test_billed_spend_none_when_billing_repo_raises():
    """Finding #3 regression: a DDB throttle on the billing-account read must
    degrade to None, not propagate — this module's "never raises" contract
    covers the repo read too, not just the Metronome call."""
    with (
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("DDB throttled")),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_summary_byok_unknown_and_oauth_are_tokens_only():
    """chatgpt_oauth used to probe the container's models.authStatus RPC for
    quota windows; the Hermes bridge never returns a usage block, so that
    read was always None while still waking a microVM per read (ponytail
    audit #14, 2026-09-23) -- it now falls straight through like every other
    non-bedrock provider, no RPC call at all."""
    assert await spend_service.get_spend_summary("owner-1", "byo_key") == {"mode": "tokens_only"}
    assert await spend_service.get_spend_summary("owner-1", None) == {"mode": "tokens_only"}
    assert await spend_service.get_spend_summary("owner-1", "chatgpt_oauth") == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_bedrock_trial_is_tokens_only():
    """resolve_provider_choice resolves trial_local owners to the
    bedrock_trial config mode (never a stored provider_choice) — this
    module has no branch for it, and it must fall through to tokens_only
    rather than get treated as billed bedrock_claude spend."""
    assert await spend_service.get_spend_summary("owner-1", "bedrock_trial") == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_bedrock_uses_billed_spend():
    billed = spend_service.BilledSpend(total_cents=1234, by_model=[], period_start="2026-07-01")
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=billed)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {
        "mode": "bedrock",
        "billed_month_to_date_cents": 1234,
        "by_model": [],
        "period_start": "2026-07-01",
    }


@pytest.mark.asyncio
async def test_summary_bedrock_degrades_when_metronome_unavailable():
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=None)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {"mode": "tokens_only"}
