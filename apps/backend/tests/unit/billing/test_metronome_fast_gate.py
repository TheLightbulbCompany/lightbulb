"""Unit tests for the Metronome local fast-gate flag (moto-mocked DDB).

The flag is webhook-fed + lazily refreshed (set_balance is the only writer);
there is no backend deplete and no optimistic top-up bump (payment-gated
commits have no synchronous confirmation), so neither path exists to test.
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

    @pytest.mark.asyncio
    async def test_set_balance_preserves_bedrock_gate_reservation(self, fast_gate_table):
        # The bedrock-gate Lambda keeps a live reserved_microcents hold on this
        # SAME row. A balance refresh (webhook / lazy read) must UPDATE remaining
        # without clobbering an outstanding hold — a whole-row put_item would
        # drop it and later settlement would over-admit paid credits.
        fast_gate_table.put_item(
            TableName="test-metronome-fast-gate",
            Item={
                "owner_id": {"S": "org_abc"},
                "remaining_microcents": {"N": "100000"},
                "reserved_microcents": {"N": "30000"},
            },
        )
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=90_000)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_abc"}},
        )["Item"]
        assert item["remaining_microcents"]["N"] == "90000"
        assert item["reserved_microcents"]["N"] == "30000"  # hold survived the refresh


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
