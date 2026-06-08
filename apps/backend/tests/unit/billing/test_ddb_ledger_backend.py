"""Unit tests for DdbLedgerBackend (moto-mocked DynamoDB)."""

import boto3
import pytest
from moto import mock_aws

from core.billing.ddb_ledger_backend import DdbLedgerBackend
from core.billing.usage_event import UsageEvent


@pytest.fixture
def ledger_tables(monkeypatch):
    """Provision moto-mocked credits + credit-transactions tables (mirrors
    tests/unit/services/test_credit_ledger.py)."""
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
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        yield client


def _seed_balance(client, owner_id: str, microcents: int) -> None:
    client.put_item(
        TableName="test-credits",
        Item={"user_id": {"S": owner_id}, "balance_microcents": {"N": str(microcents)}},
    )


def _event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-1",
        model_id="aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_000,
        billed_microcents=14_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_zero_for_new_owner(self, ledger_tables):
        assert await DdbLedgerBackend().get_balance("org_none") == 0

    @pytest.mark.asyncio
    async def test_reads_seeded_balance(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 50_000)
        assert await DdbLedgerBackend().get_balance("org_abc") == 50_000

    @pytest.mark.asyncio
    async def test_consistent_read_param_accepted(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 7)
        assert await DdbLedgerBackend().get_balance("org_abc", consistent=True) == 7
