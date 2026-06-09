"""Unit tests for the normalized UsageEvent (meter -> ledger contract)."""

import dataclasses

import pytest

from core.billing.usage_event import UsageEvent


def _make_event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-123",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_500,
        billed_microcents=14_700,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


def test_usage_event_holds_all_fields():
    e = _make_event()
    assert e.owner_id == "org_abc"
    assert e.request_id == "req-123"
    assert e.billed_microcents == 14_700
    assert e.raw_cost_microcents == 10_500
    assert e.run_status == "end"


def test_usage_event_is_frozen():
    e = _make_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.billed_microcents = 0  # type: ignore[misc]


def test_usage_event_rejects_blank_request_id():
    with pytest.raises(ValueError, match="request_id"):
        _make_event(request_id="")


def test_usage_event_rejects_blank_owner_id():
    with pytest.raises(ValueError, match="owner_id"):
        _make_event(owner_id="")


def test_usage_event_rejects_negative_billed():
    with pytest.raises(ValueError, match="billed_microcents"):
        _make_event(billed_microcents=-1)
