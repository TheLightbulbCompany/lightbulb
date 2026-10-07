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
async def test_record_usage_records_volume_with_zero_spend(dynamodb_tables):
    """Every model records token + request VOLUME with spend 0: managed OpenRouter spend is the
    settlement Lambda's to write, and ChatGPT/BYO inference is user-paid."""
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
async def test_record_usage_empty_call_records_nothing(dynamodb_tables):
    """A completely-empty event (no tokens, unknown model → spend 0) is skipped."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage("org_empty", "user_e", "unknown-model", 0, 0, 0, 0)

    period = _current_period()
    assert await usage_repo.get_period_usage("org_empty", period) is None
    assert await usage_repo.get_period_usage("org_empty", "lifetime") is None


@pytest.mark.asyncio
async def test_room_turn_writes_an_event_kind_row_beside_the_rollups(dynamodb_tables):
    """Ambient (room_event) and addressed (user_request) room spend have to be
    readable apart; the owner/lifetime/member rollups still carry every turn."""
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    common = dict(
        owner_id="org_room",
        user_id="user_a",
        model="anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
        room_turn=True,
    )
    await record_usage(**common, event_kind="user_request")
    await record_usage(**common, event_kind="room_event")
    await record_usage(**common, event_kind="room_event")

    period = _current_period()
    addressed = await usage_repo.get_period_usage("org_room", f"room:user_request:{period}")
    ambient = await usage_repo.get_period_usage("org_room", f"room:room_event:{period}")
    owner = await usage_repo.get_period_usage("org_room", period)

    assert addressed is not None and addressed["request_count"] == 1
    assert ambient is not None and ambient["request_count"] == 2
    assert owner is not None and owner["request_count"] == 3
    # The split rows must not leak into the per-member breakdown.
    assert [m["user_id"] for m in await usage_repo.get_member_usage("org_room", period)] == ["user_a"]


@pytest.mark.asyncio
async def test_non_room_turn_writes_no_extra_row(dynamodb_tables):
    from core.repositories import usage_repo
    from core.services.usage_service import _current_period, record_usage

    await record_usage(
        owner_id="org_web",
        user_id="user_a",
        model="anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_write=0,
    )

    period = _current_period()
    assert await usage_repo.get_period_usage("org_web", f"room:unknown:{period}") is None
    assert await usage_repo.count_for_owner("org_web") == 3


@pytest.mark.asyncio
async def test_get_usage_summary_returns_zeros_for_empty(dynamodb_tables):
    from core.services.usage_service import get_usage_summary

    summary = await get_usage_summary("user_with_no_usage")
    assert summary["total_spend"] == 0
    assert summary["request_count"] == 0
    assert summary["lifetime_spend"] == 0
