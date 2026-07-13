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
    with (
        _patch_lifecycle(True),
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
    with (
        _patch_lifecycle(True),
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
        _patch_lifecycle(True),
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
        _patch_lifecycle(True),
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
async def test_billed_spend_none_when_billing_repo_raises():
    """Finding #3 regression: a DDB throttle on the billing-account read must
    degrade to None, not propagate — this module's "never raises" contract
    covers the repo read too, not just the Metronome call."""
    with (
        _patch_lifecycle(True),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("DDB throttled")),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


AUTH_STATUS_RESULT = {
    "ts": 1,
    "providers": [
        {"provider": "anthropic"},
        {
            "provider": "openai",
            "usage": {
                "plan": "pro",
                "windows": [
                    {"label": "5h", "usedPercent": 43, "resetAt": 1789000000000},
                    {"label": "Week", "usedPercent": 61},
                ],
            },
        },
    ],
}


def _patch_gateway(rpc_result=None, rpc_error=None, container=("c", "10.0.0.1")):
    """Patch the ecs/pool accessors spend_service uses for the OAuth branch."""
    ecs = AsyncMock()
    ecs.resolve_running_container.return_value = ({"gateway_token": "tok"}, container[1]) if container else (None, None)
    pool = AsyncMock()
    if rpc_error:
        pool.send_rpc.side_effect = rpc_error
    else:
        pool.send_rpc.return_value = rpc_result
    return (
        patch.object(spend_service, "_ecs_manager", return_value=ecs),
        patch.object(spend_service, "_gateway_pool", return_value=pool),
    )


@pytest.mark.asyncio
async def test_summary_oauth_maps_quota_windows():
    p1, p2 = _patch_gateway(rpc_result=AUTH_STATUS_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload["mode"] == "oauth_quota"
    assert payload["plan"] == "pro"
    assert payload["windows"] == [
        {"label": "5h", "used_percent": 43, "resets_at_ms": 1789000000000},
        {"label": "Week", "used_percent": 61, "resets_at_ms": None},
    ]


@pytest.mark.asyncio
async def test_summary_oauth_degrades_to_tokens_only_on_rpc_failure():
    p1, p2 = _patch_gateway(rpc_error=RuntimeError("container gone"))
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_oauth_degrades_when_no_container():
    p1, p2 = _patch_gateway(container=None)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_byok_and_unknown_are_tokens_only():
    assert await spend_service.get_spend_summary("owner-1", "byo_key") == {"mode": "tokens_only"}
    assert await spend_service.get_spend_summary("owner-1", None) == {"mode": "tokens_only"}


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
