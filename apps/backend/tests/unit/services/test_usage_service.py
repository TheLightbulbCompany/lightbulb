"""Tests for usage service — counter writes for analytics."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_tables():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-usage-counters",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "period", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "period", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield


@pytest.mark.asyncio
async def test_record_usage_increments_owner_lifetime_and_member(dynamodb_tables):
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_1",
        user_id="user_a",
        model="anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
    )

    period = _current_period()

    owner_usage = await usage_repo.get_period_usage("org_1", period)
    assert owner_usage is not None
    assert owner_usage["request_count"] == 1

    lifetime = await usage_repo.get_period_usage("org_1", "lifetime")
    assert lifetime is not None

    member_usage = await usage_repo.get_period_usage("org_1", f"member:user_a:{period}")
    assert member_usage is not None
    assert member_usage["request_count"] == 1


@pytest.mark.asyncio
async def test_record_usage_strips_provider_prefix(dynamodb_tables):
    """Models from chat.final arrive prefixed (``amazon-bedrock/``); the prefix
    must be stripped before pricing lookup."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_2",
        user_id="user_b",
        model="amazon-bedrock/anthropic.claude-opus-4-6-v1",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
    )
    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_2", period)
    assert owner_usage is not None
    assert owner_usage["request_count"] == 1


@pytest.mark.asyncio
async def test_record_usage_unknown_model_records_volume_with_zero_spend(dynamodb_tables):
    """Non-Bedrock models (OpenAI/Codex) have no Bedrock pricing entry. We still
    record token + request VOLUME (with spend 0) so usage is never invisible —
    spend 0 is correct for Lightbulb AI's books (byo_key is user-paid, Codex is
    flat-rate)."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    # Should not raise
    await record_usage("org_codex", "user_c", "openai/gpt-5.5", 1000, 500, 200, 100)

    period = _current_period()

    for owner_id, sk in [
        ("org_codex", period),
        ("org_codex", "lifetime"),
        ("org_codex", f"member:user_c:{period}"),
    ]:
        usage = await usage_repo.get_period_usage(owner_id, sk)
        assert usage is not None, f"expected volume recorded for {sk}"
        assert usage["request_count"] == 1
        assert usage["total_input_tokens"] == 1000
        assert usage["total_output_tokens"] == 500
        assert usage["total_cache_read_tokens"] == 200
        assert usage["total_cache_write_tokens"] == 100
        assert usage["total_spend_microdollars"] == 0


@pytest.mark.asyncio
async def test_record_usage_known_model_records_spend(dynamodb_tables):
    """Regression: a known Bedrock model still records real (non-zero) spend."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_bedrock",
        user_id="user_d",
        model="anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_bedrock", period)
    assert owner_usage is not None
    assert owner_usage["request_count"] == 1
    assert owner_usage["total_input_tokens"] == 1000
    assert owner_usage["total_output_tokens"] == 500
    assert owner_usage["total_spend_microdollars"] > 0


@pytest.mark.asyncio
async def test_unpriced_bedrock_model_pages_and_records_volume(dynamodb_tables):
    """A Bedrock model with no rate row is a genuine pricing gap: emit the
    page-tier ``billing.pricing.missing_model`` metric AND still record token
    volume with spend 0."""
    from core.repositories import usage_repo
    from core.services import usage_service
    from core.services.usage_service import _current_period, record_usage

    with patch.object(usage_service, "put_metric") as mock_put_metric:
        await record_usage(
            owner_id="org_bedrock_gap",
            user_id="user_f",
            model="amazon-bedrock/us.anthropic.claude-some-unpriced",
            input_tokens=1000,
            output_tokens=500,
            cache_read=0,
            cache_write=0,
        )

    metric_names = [call.args[0] for call in mock_put_metric.call_args_list]
    assert "billing.pricing.missing_model" in metric_names

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_bedrock_gap", period)
    assert owner_usage is not None
    assert owner_usage["request_count"] == 1
    assert owner_usage["total_input_tokens"] == 1000
    assert owner_usage["total_output_tokens"] == 500
    assert owner_usage["total_spend_microdollars"] == 0


@pytest.mark.asyncio
async def test_non_bedrock_model_does_not_page_but_records_volume(dynamodb_tables):
    """A non-Bedrock model (OpenAI/Codex/BYO Anthropic) has no Bedrock pricing
    row by design. It must NOT emit the page-tier metric, but token volume is
    still recorded with spend 0."""
    from core.repositories import usage_repo
    from core.services import usage_service
    from core.services.usage_service import _current_period, record_usage

    with patch.object(usage_service, "put_metric") as mock_put_metric:
        await record_usage(
            owner_id="org_codex_gap",
            user_id="user_g",
            model="openai/gpt-5.5",
            input_tokens=1000,
            output_tokens=500,
            cache_read=0,
            cache_write=0,
        )

    metric_names = [call.args[0] for call in mock_put_metric.call_args_list]
    assert "billing.pricing.missing_model" not in metric_names

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_codex_gap", period)
    assert owner_usage is not None
    assert owner_usage["request_count"] == 1
    assert owner_usage["total_input_tokens"] == 1000
    assert owner_usage["total_output_tokens"] == 500
    assert owner_usage["total_spend_microdollars"] == 0


@pytest.mark.asyncio
async def test_record_usage_empty_call_records_nothing(dynamodb_tables):
    """A completely-empty event (no tokens, unknown model → spend 0) is skipped."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage("org_empty", "user_e", "unknown-model", 0, 0, 0, 0)

    period = _current_period()
    assert await usage_repo.get_period_usage("org_empty", period) is None
    assert await usage_repo.get_period_usage("org_empty", "lifetime") is None


@pytest.mark.asyncio
async def test_record_usage_raw_cost_override_used_verbatim(dynamodb_tables):
    """When the caller passes ``raw_cost_microcents`` (sourced from OpenClaw's
    cache-inclusive ``estimatedCostUsd``) for a Bedrock model, it is recorded
    verbatim as spend — NOT recomputed from the cache-blind token counts. The
    token counts are still recorded for analytics. This is the ~30x undercount
    fix: cache tokens are ~99.99% of volume but sessions.list omits them, so the
    token-based path captures only ~3% of real cost."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    # estimatedCostUsd = 1.234567 USD → 1_234_567 microcents.
    # Token counts are tiny (cache-blind), so the token-based path would
    # record a far smaller spend — the override must win. The model carries
    # the amazon-bedrock/ prefix because the override is gated to Bedrock.
    await record_usage(
        owner_id="org_override",
        user_id="user_o",
        model="amazon-bedrock/us.anthropic.claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=5,
        cache_read=0,
        cache_write=0,
        raw_cost_microcents=1_234_567,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_override", period)
    assert owner_usage is not None
    assert owner_usage["total_spend_microdollars"] == 1_234_567
    # Token volume is still recorded for analytics.
    assert owner_usage["total_input_tokens"] == 10
    assert owner_usage["total_output_tokens"] == 5


@pytest.mark.asyncio
async def test_record_usage_raw_cost_override_records_when_tokens_zero(dynamodb_tables):
    """A cache-heavy Bedrock turn may report ~0 input/output tokens (cache counts
    are omitted by sessions.list) but a real positive cost. With a raw-cost
    override the event must still be recorded, not dropped by the
    empty-event guard."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_override_zero_tok",
        user_id="user_z",
        model="amazon-bedrock/us.anthropic.claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_write=0,
        raw_cost_microcents=900_000,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_override_zero_tok", period)
    assert owner_usage is not None
    assert owner_usage["total_spend_microdollars"] == 900_000


@pytest.mark.asyncio
async def test_record_usage_raw_cost_override_used_for_bedrock_model(dynamodb_tables):
    """A Bedrock model carrying ``raw_cost_microcents`` records the override
    verbatim as spend. The override is the cache-inclusive cost from OpenClaw;
    gating it to Bedrock keeps this (the priced/Lightbulb AI-billed path) intact."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_bedrock_override",
        user_id="user_bo",
        model="amazon-bedrock/us.anthropic.claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=5,
        cache_read=0,
        cache_write=0,
        raw_cost_microcents=1_234_567,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_bedrock_override", period)
    assert owner_usage is not None
    assert owner_usage["total_spend_microdollars"] == 1_234_567
    assert owner_usage["total_input_tokens"] == 10
    assert owner_usage["total_output_tokens"] == 5


@pytest.mark.asyncio
async def test_record_usage_raw_cost_override_ignored_for_non_bedrock_model(dynamodb_tables):
    """A non-Bedrock model (ChatGPT OAuth / OpenAI / BYO) that carries a
    ``raw_cost_microcents`` (from OpenClaw's ``estimatedCostUsd``) must NOT
    record it as Lightbulb AI spend. Non-Bedrock = user-paid / flat-rate, so Lightbulb AI's
    books read spend 0. The override is gated to Bedrock; a non-Bedrock model
    falls through to the token path → UnknownModelError → spend 0, even with
    the override present. Token volume is still recorded."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_codex_override",
        user_id="user_co",
        model="openai/gpt-5.5",
        input_tokens=1000,
        output_tokens=500,
        cache_read=200,
        cache_write=100,
        raw_cost_microcents=1_234_567,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_codex_override", period)
    assert owner_usage is not None
    # Override IGNORED for the non-Bedrock model — spend stays 0.
    assert owner_usage["total_spend_microdollars"] == 0
    # Token volume still recorded for analytics.
    assert owner_usage["request_count"] == 1
    assert owner_usage["total_input_tokens"] == 1000
    assert owner_usage["total_output_tokens"] == 500
    assert owner_usage["total_cache_read_tokens"] == 200
    assert owner_usage["total_cache_write_tokens"] == 100


@pytest.mark.asyncio
async def test_record_usage_none_override_falls_back_to_token_pricing(dynamodb_tables):
    """When ``raw_cost_microcents`` is None (the default — no estimatedCostUsd
    on the row), the existing token-based bedrock_pricing path is used,
    unchanged."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_fallback",
        user_id="user_fb",
        model="anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
        raw_cost_microcents=None,
    )

    period = _current_period()
    owner_usage = await usage_repo.get_period_usage("org_fallback", period)
    assert owner_usage is not None
    # 1000 input * 3.3 + 500 output * 16.5 (USD per MTok) → token-based spend.
    assert owner_usage["total_spend_microdollars"] == 11_550


@pytest.mark.asyncio
async def test_get_usage_summary_returns_zeros_for_empty(dynamodb_tables):
    from core.services.usage_service import get_usage_summary

    summary = await get_usage_summary("user_with_no_usage")
    assert summary["total_spend"] == 0
    assert summary["request_count"] == 0
    assert summary["lifetime_spend"] == 0
