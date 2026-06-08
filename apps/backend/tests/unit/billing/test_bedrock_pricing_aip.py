"""Tests for price_from_aip_arn — AIP ARN -> tier -> FM id -> cost.

NOTE (plan/reality reconciliation): the Plan-3 doc declared price_from_aip_arn
synchronous, patching resolve_tier_from_aip_arn with a plain return_value. The
merged Plan-2 resolve_tier_from_aip_arn is an ``async def`` (DynamoDB GetItem),
so price_from_aip_arn is async + awaits it, and these tests patch the resolver
with an AsyncMock and await the helper. Arithmetic + behavior are otherwise
exactly as the plan specifies.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.billing import bedrock_pricing
from core.billing.bedrock_pricing import UnknownModelError, price_from_aip_arn, price_from_tier

OPUS_AIP = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc"


def test_price_from_tier_opus():
    # 1000 input * 5.5 + 500 output * 27.5 = 5500 + 13750 = 19250 microcents
    cost = price_from_tier("opus", input_tokens=1000, output_tokens=500)
    assert cost == 19250


def test_price_from_tier_haiku_with_cache():
    # haiku: 100*1.1 + 100*5.5 + 50*0.11(cache_read) + 20*2.2(cache_write)
    #      = 110 + 550 + 5 + 44 = 709
    cost = price_from_tier(
        "haiku",
        input_tokens=100,
        output_tokens=100,
        cache_read_tokens=50,
        cache_write_tokens=20,
    )
    assert cost == 709


def test_price_from_tier_unknown_tier_raises():
    with pytest.raises(UnknownModelError):
        price_from_tier("nope", input_tokens=10, output_tokens=10)
    with pytest.raises(UnknownModelError):
        price_from_tier(None, input_tokens=10, output_tokens=10)


@pytest.mark.asyncio
async def test_price_from_aip_arn_opus_via_tier():
    # 1000 input * 5.5 + 500 output * 27.5 = 5500 + 13750 = 19250 microcents
    with patch.object(
        bedrock_pricing,
        "resolve_tier_from_aip_arn",
        AsyncMock(return_value="opus"),
    ):
        cost = await price_from_aip_arn(
            aip_arn=OPUS_AIP,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    assert cost == 19250


@pytest.mark.asyncio
async def test_price_from_aip_arn_includes_cache_tokens():
    # opus: 1000*5.5 + 0 + 2000*0.55(cache_read) + 300*11.0(cache_write)
    #     = 5500 + 1100 + 3300 = 9900
    with patch.object(
        bedrock_pricing,
        "resolve_tier_from_aip_arn",
        AsyncMock(return_value="opus"),
    ):
        cost = await price_from_aip_arn(
            aip_arn=OPUS_AIP,
            input_tokens=1000,
            output_tokens=0,
            cache_read_tokens=2000,
            cache_write_tokens=300,
        )
    assert cost == 9900


@pytest.mark.asyncio
async def test_price_from_aip_arn_haiku_tier():
    # haiku: 100*1.1 + 100*5.5 = 110 + 550 = 660
    with patch.object(
        bedrock_pricing,
        "resolve_tier_from_aip_arn",
        AsyncMock(return_value="haiku"),
    ):
        cost = await price_from_aip_arn(
            aip_arn="arn:...:application-inference-profile/aip-haiku-x",
            input_tokens=100,
            output_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    assert cost == 660


@pytest.mark.asyncio
async def test_price_from_aip_arn_unknown_tier_raises():
    with patch.object(
        bedrock_pricing,
        "resolve_tier_from_aip_arn",
        AsyncMock(return_value=None),
    ):
        with pytest.raises(UnknownModelError):
            await price_from_aip_arn(
                aip_arn="arn:...:application-inference-profile/aip-mystery",
                input_tokens=10,
                output_tokens=10,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
