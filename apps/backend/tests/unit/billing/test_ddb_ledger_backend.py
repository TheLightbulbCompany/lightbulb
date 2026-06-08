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


class TestDeplete:
    @pytest.mark.asyncio
    async def test_deplete_applies_once(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        result = await DdbLedgerBackend().deplete(_event(billed_microcents=14_000))
        assert result.applied is True
        assert result.balance_microcents == 86_000
        assert await DdbLedgerBackend().get_balance("org_abc", consistent=True) == 86_000

    @pytest.mark.asyncio
    async def test_deplete_writes_audit_row_keyed_on_request_id(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        await DdbLedgerBackend().deplete(_event(request_id="req-XYZ"))
        resp = ledger_tables.get_item(
            TableName="test-credit-txns",
            Key={"user_id": {"S": "org_abc"}, "tx_id": {"S": "deduct#req-XYZ"}},
        )
        item = resp["Item"]
        assert item["type"]["S"] == "deduct"
        assert item["amount_microcents"]["N"] == "-14000"
        assert item["raw_cost_microcents"]["N"] == "10000"
        assert item["bedrock_invocation_id"]["S"] == "req-XYZ"
        assert item["model_id"]["S"] == "aip-xyz"
        assert item["input_tokens"]["N"] == "1000"
        assert item["cache_read_tokens"]["N"] == "2000"

    @pytest.mark.asyncio
    async def test_deplete_is_idempotent_on_replay(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        backend = DdbLedgerBackend()
        first = await backend.deplete(_event(request_id="req-dup", billed_microcents=14_000))
        second = await backend.deplete(_event(request_id="req-dup", billed_microcents=14_000))
        assert first.applied is True
        assert second.applied is False
        # Deducted exactly once.
        assert second.balance_microcents == 86_000
        assert await backend.get_balance("org_abc", consistent=True) == 86_000

    @pytest.mark.asyncio
    async def test_deplete_allows_negative_balance(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 5_000)
        result = await DdbLedgerBackend().deplete(_event(billed_microcents=14_000))
        assert result.applied is True
        assert result.balance_microcents == -9_000

    @pytest.mark.asyncio
    async def test_deplete_zero_billed_still_writes_dedup_row(self, ledger_tables):
        # A genuinely free run (billed 0) still records its request_id so a
        # replay is a no-op and the audit log is complete.
        _seed_balance(ledger_tables, "org_abc", 100_000)
        backend = DdbLedgerBackend()
        r1 = await backend.deplete(_event(request_id="req-free", billed_microcents=0))
        r2 = await backend.deplete(_event(request_id="req-free", billed_microcents=0))
        assert r1.applied is True
        assert r2.applied is False
        assert await backend.get_balance("org_abc", consistent=True) == 100_000

    @pytest.mark.asyncio
    async def test_deplete_reraises_non_dedup_cancellation(self, ledger_tables, monkeypatch):
        # A TransactionCanceledException whose Put (dedup) item did NOT fail on
        # its condition is a REAL error (e.g. the balance Update was cancelled),
        # NOT a replay — it must propagate, never be silently swallowed as
        # applied=False. This is the safety-critical "don't mask a real failure
        # as a dedup" branch (ddb_ledger_backend.py: the `put_reason !=
        # "ConditionalCheckFailed" -> raise` guard).
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        synthetic = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "boom"},
                # Index 0 = the balance Update (failed); index 1 = the Put, which
                # did NOT fail its dedup condition -> this is not a replay.
                "CancellationReasons": [
                    {"Code": "ConditionalCheckFailed"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )
        fake_client = MagicMock()
        fake_client.transact_write_items.side_effect = synthetic
        monkeypatch.setattr("core.billing.ddb_ledger_backend._ddb_client", lambda: fake_client)

        with pytest.raises(ClientError) as exc_info:
            await DdbLedgerBackend().deplete(_event(request_id="req-realerror"))
        assert exc_info.value.response["Error"]["Code"] == "TransactionCanceledException"
