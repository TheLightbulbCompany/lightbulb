"""Unit tests for the ledger interface: DepleteResult + get_ledger() factory."""

import boto3
import pytest
from moto import mock_aws

from core.billing.ddb_ledger_backend import DdbLedgerBackend
from core.billing.ledger_interface import DepleteResult, get_ledger
from core.billing.usage_event import UsageEvent


def test_deplete_result_fields():
    r = DepleteResult(balance_microcents=500, applied=True)
    assert r.balance_microcents == 500
    assert r.applied is True


def test_get_ledger_returns_metronome_backend_by_default(monkeypatch):
    # Phase 3 cutover: the config default flipped to "metronome".
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

    ledger = get_ledger()
    assert isinstance(ledger, MetronomeLedgerBackend)


def test_get_ledger_honours_ddb_setting(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "ddb")
    assert isinstance(get_ledger(), DdbLedgerBackend)


def test_get_ledger_returns_metronome_backend_when_selected(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

    ledger = get_ledger()
    assert isinstance(ledger, MetronomeLedgerBackend)


def test_get_ledger_still_defaults_to_ddb(monkeypatch):
    # Phase 2 must NOT change the default — cutover is Phase 3.
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "ddb")
    assert isinstance(get_ledger(), DdbLedgerBackend)


def test_get_ledger_still_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        get_ledger()


@pytest.fixture
def ledger_tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="test-credits",
            Item={"user_id": {"S": "org_seam"}, "balance_microcents": {"N": "50000"}},
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        monkeypatch.setenv("LEDGER_BACKEND", "ddb")
        yield client


@pytest.mark.asyncio
async def test_get_ledger_deplete_then_balance_through_interface(ledger_tables):
    ledger = get_ledger()
    event = UsageEvent(
        owner_id="org_seam",
        request_id="req-seam",
        model_id="aip-1",
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        raw_cost_microcents=5_000,
        billed_microcents=7_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    result = await ledger.deplete(event)
    assert result.applied is True
    assert result.balance_microcents == 43_000
    assert await ledger.get_balance("org_seam", consistent=True) == 43_000
