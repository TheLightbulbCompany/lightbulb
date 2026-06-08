"""Unit tests for MetronomeLedgerBackend (mocked client + mocked fast-gate)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.billing.ledger_interface import DepleteResult
from core.billing.metronome_ledger_backend import MetronomeLedgerBackend
from core.billing.usage_event import UsageEvent


def _event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-1",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_000,
        billed_microcents=14_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-08T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


def _backend(*, ingest_return=None, net_balance_data=None, decrement_return=86_000):
    fake_client = MagicMock()
    fake_client.ingest = AsyncMock(return_value=ingest_return)
    fake_client.get_net_balance = AsyncMock(return_value=net_balance_data or {})
    fake_gate = MagicMock()
    fake_gate.decrement = AsyncMock(return_value=decrement_return)
    fake_gate.set_balance = AsyncMock()

    backend = MetronomeLedgerBackend(
        client_factory=lambda http: fake_client,
        fast_gate=fake_gate,
    )
    return backend, fake_client, fake_gate


class TestDeplete:
    @pytest.mark.asyncio
    async def test_deplete_ingests_one_event_with_request_id_as_transaction_id(self):
        backend, client, _ = _backend()
        await backend.deplete(_event(request_id="req-XYZ"))

        client.ingest.assert_awaited_once()
        (events,), _ = client.ingest.call_args
        assert len(events) == 1
        ev = events[0]
        assert ev["transaction_id"] == "req-XYZ"
        assert ev["customer_id"] == "org_abc"  # ingest alias = owner_id
        assert ev["event_type"] == "bedrock_invocation"
        assert ev["timestamp"] == "2026-06-08T12:00:00Z"

    @pytest.mark.asyncio
    async def test_deplete_stringifies_all_properties(self):
        backend, client, _ = _backend()
        await backend.deplete(_event())
        (events,), _ = client.ingest.call_args
        props = events[0]["properties"]
        # Metronome contract: all property values must be strings.
        assert all(isinstance(v, str) for v in props.values())
        assert props["input_tokens"] == "1000"
        assert props["output_tokens"] == "500"
        assert props["cache_read_tokens"] == "2000"
        assert props["cache_write_tokens"] == "300"
        assert props["raw_cost_microcents"] == "10000"
        assert props["billed_microcents"] == "14000"
        assert props["markup_multiplier"] == "1.4"
        assert props["model_id"].endswith("aip-xyz")
        assert props["region"] == "us-east-1"
        assert props["run_status"] == "end"
        assert props["source"] == "bedrock_invocation_log"

    @pytest.mark.asyncio
    async def test_deplete_decrements_fast_gate_by_billed_amount(self):
        backend, _, gate = _backend(decrement_return=72_000)
        result = await backend.deplete(_event(billed_microcents=14_000))
        gate.decrement.assert_awaited_once_with("org_abc", microcents=14_000)
        assert isinstance(result, DepleteResult)
        assert result.balance_microcents == 72_000
        assert result.applied is True

    @pytest.mark.asyncio
    async def test_deplete_bills_error_runs(self):
        # Spec §3.2.2: bill on error/aborted too — no status gate.
        backend, client, gate = _backend()
        await backend.deplete(_event(run_status="error"))
        client.ingest.assert_awaited_once()
        gate.decrement.assert_awaited_once()


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_get_balance_converts_dollars_to_microcents(self):
        # 12.34 dollars -> 12_340_000 microcents (1 dollar = 1_000_000 mc).
        backend, _, _ = _backend(net_balance_data={"net_balance": 12.34})
        assert await backend.get_balance("org_abc") == 12_340_000

    @pytest.mark.asyncio
    async def test_get_balance_zero_when_no_balance(self):
        backend, _, _ = _backend(net_balance_data={})
        assert await backend.get_balance("org_abc") == 0

    @pytest.mark.asyncio
    async def test_get_balance_accepts_consistent_kwarg(self):
        backend, _, _ = _backend(net_balance_data={"net_balance": 1.0})
        assert await backend.get_balance("org_abc", consistent=True) == 1_000_000
