"""Unit tests for the Metronome local fast-gate flag (moto-mocked DDB).

The flag is webhook-fed (set_balance) + top-up re-armed (add_credit); there
is no backend deplete, so no decrement path exists to test.
"""

import time

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_fast_gate import MetronomeFastGate, is_stale


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
        # No projection yet -> not gated (the webhook/top-up will create the row).
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
        # The webhook can report a balance that already went negative
        # (Metronome rates asynchronously); the cache reflects it verbatim.
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=-4_000)
        assert await gate.get_remaining("org_abc") == -4_000
        assert await gate.is_blocked("org_abc") is True


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


class TestGetRow:
    @pytest.mark.asyncio
    async def test_missing_owner_returns_none(self, fast_gate_table):
        assert await MetronomeFastGate().get_row("org_new") is None

    @pytest.mark.asyncio
    async def test_returns_full_projection_row(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=7_000)
        row = await gate.get_row("org_abc")
        assert row is not None
        assert int(row["remaining_microcents"]) == 7_000
        # updated_at rides along so the caller can feed is_stale.
        assert isinstance(row["updated_at"], str)


class TestAddCredit:
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


class TestIsStale:
    def test_missing_row_is_stale(self):
        assert is_stale(None, 300) is True
        assert is_stale({}, 300) is True

    def test_missing_or_garbled_timestamp_is_stale(self):
        # A row with no parseable updated_at can't prove freshness — the safe
        # refresh decision is "go ask Metronome".
        assert is_stale({"owner_id": "org_abc"}, 300) is True
        assert is_stale({"owner_id": "org_abc", "updated_at": 1234}, 300) is True
        assert is_stale({"owner_id": "org_abc", "updated_at": "not-a-time"}, 300) is True

    def test_fresh_row_is_not_stale(self):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assert is_stale({"updated_at": now_iso}, 300) is False

    def test_old_row_is_stale(self):
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3_600))
        assert is_stale({"updated_at": old_iso}, 300) is True

    @pytest.mark.asyncio
    async def test_round_trips_with_set_balance(self, fast_gate_table):
        # The row set_balance writes is immediately fresh under any sane window.
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=1_000)
        row = await gate.get_row("org_abc")
        assert is_stale(row, 300) is False
