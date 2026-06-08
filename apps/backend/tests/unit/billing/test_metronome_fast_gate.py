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


class TestGetRemaining:
    @pytest.mark.asyncio
    async def test_missing_owner_returns_zero(self, fast_gate_table):
        # No projection yet -> 0 spendable; the synchronous gate treats a
        # brand-new owner as not-yet-metered (is_blocked is the gate, and it
        # returns False on a missing row).
        assert await MetronomeFastGate().get_remaining("org_new") == 0

    @pytest.mark.asyncio
    async def test_returns_cached_remaining(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=42_000)
        assert await MetronomeFastGate().get_remaining("org_abc") == 42_000

    @pytest.mark.asyncio
    async def test_returns_negative_remaining(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=1_000)
        await gate.decrement("org_abc", microcents=5_000)
        assert await gate.get_remaining("org_abc") == -4_000


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

    @pytest.mark.asyncio
    async def test_add_credit_rearms_a_blocked_owner(self, fast_gate_table):
        # A confirmed Stripe top-up optimistically re-arms the cache so the
        # owner's NEXT chat message unblocks immediately, without waiting for a
        # Metronome balance webhook (which won't fire on a top-up). Safe
        # direction: the payment is already confirmed when this is called.
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=0)  # blocked
        assert await gate.is_blocked("org_abc") is True
        new_remaining = await gate.add_credit("org_abc", microcents=25_000_000)
        assert new_remaining == 25_000_000
        assert await gate.is_blocked("org_abc") is False

    @pytest.mark.asyncio
    async def test_add_credit_missing_owner_creates_positive_row(self, fast_gate_table):
        gate = MetronomeFastGate()
        new_remaining = await gate.add_credit("org_first_topup", microcents=10_000)
        assert new_remaining == 10_000
        assert await gate.is_blocked("org_first_topup") is False

    @pytest.mark.asyncio
    async def test_decrement_is_single_atomic_write(self, fast_gate_table):
        # decrement issues exactly ONE update_item so a meter-Lambda DLQ
        # redrive (which re-runs decrement after a partial failure) cannot
        # double-decrement via a second SET-blocked round trip. is_blocked
        # derives from remaining_microcents, so the row needs no persisted
        # `blocked` attribute written by decrement.
        gate = MetronomeFastGate()
        await gate.decrement("org_only_remaining", microcents=3_000)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_only_remaining"}},
        )["Item"]
        assert item["remaining_microcents"]["N"] == "-3000"
        # decrement does not persist `blocked`; is_blocked computes it.
        assert "blocked" not in item
        assert await gate.is_blocked("org_only_remaining") is True
