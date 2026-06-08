"""Unit tests for the Metronome local fast-gate flag (moto-mocked DDB)."""

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_fast_gate import MetronomeFastGate


@pytest.fixture
def fast_gate_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


class TestIsBlocked:
    @pytest.mark.asyncio
    async def test_missing_owner_is_not_blocked(self, fast_gate_table):
        # No projection yet -> not gated (metering/funding will create the row).
        assert await MetronomeFastGate().is_blocked("org_new") is False

    @pytest.mark.asyncio
    async def test_blocked_when_flag_set(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=0)
        assert await MetronomeFastGate().is_blocked("org_abc") is True

    @pytest.mark.asyncio
    async def test_not_blocked_when_positive(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=50_000)
        assert await MetronomeFastGate().is_blocked("org_abc") is False


class TestSetBalance:
    @pytest.mark.asyncio
    async def test_set_balance_writes_blocked_and_remaining(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=5_000)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_abc"}},
        )["Item"]
        assert item["remaining_microcents"]["N"] == "5000"
        assert item["blocked"]["BOOL"] is False
        assert "updated_at" in item

    @pytest.mark.asyncio
    async def test_set_balance_zero_marks_blocked(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=0)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_abc"}},
        )["Item"]
        assert item["blocked"]["BOOL"] is True


class TestDecrement:
    @pytest.mark.asyncio
    async def test_optimistic_decrement_reduces_remaining(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=10_000)
        new_remaining = await gate.decrement("org_abc", microcents=3_000)
        assert new_remaining == 7_000
        assert await gate.is_blocked("org_abc") is False

    @pytest.mark.asyncio
    async def test_decrement_crossing_zero_blocks(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=2_000)
        new_remaining = await gate.decrement("org_abc", microcents=5_000)
        assert new_remaining == -3_000
        assert await gate.is_blocked("org_abc") is True

    @pytest.mark.asyncio
    async def test_decrement_missing_owner_creates_negative_row(self, fast_gate_table):
        # No prior projection: ADD on a missing number starts from 0, so the
        # row lands negative and blocked. Safe-by-default (a metered owner with
        # no funded balance is gated).
        gate = MetronomeFastGate()
        new_remaining = await gate.decrement("org_unfunded", microcents=4_000)
        assert new_remaining == -4_000
        assert await gate.is_blocked("org_unfunded") is True
