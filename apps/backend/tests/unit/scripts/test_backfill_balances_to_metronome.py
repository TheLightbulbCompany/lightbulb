"""Unit tests for the one-time DDB-balance -> Metronome-credit backfill.

moto-mocks the credits + fast-gate tables; mocks the Metronome funding +
provisioning HTTP so no live call is made. Asserts idempotency (a second run
grants nothing more).
"""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from scripts.backfill_balances_to_metronome import backfill_owner, run_backfill


@pytest.fixture
def tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="isol8-test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="isol8-test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # One funded owner, one zero-balance owner (skipped).
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_funded"}, "balance_microcents": {"N": "12000000"}},
        )
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_zero"}, "balance_microcents": {"N": "0"}},
        )
        # The funded owner is already Metronome-provisioned.
        client.put_item(
            TableName="isol8-test-metronome-fast-gate",
            Item={"owner_id": {"S": "org_funded"}, "metronome_customer_id": {"S": "cust-1"}},
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "isol8-test-metronome-fast-gate")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
        yield client


@pytest.mark.asyncio
async def test_backfill_owner_grants_equal_credit_and_marks_done(tables):
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is True
    mock_grant.assert_awaited_once()
    _, kwargs = mock_grant.call_args
    assert kwargs["owner_id"] == "org_funded"
    assert kwargs["amount_microcents"] == 12_000_000
    # Idempotency marker persisted on the fast-gate row.
    item = tables.get_item(TableName="isol8-test-metronome-fast-gate", Key={"owner_id": {"S": "org_funded"}})["Item"]
    assert "backfill_done_at" in item


@pytest.mark.asyncio
async def test_backfill_owner_idempotent_skips_when_marker_set(tables):
    tables.update_item(
        TableName="isol8-test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_funded"}},
        UpdateExpression="SET backfill_done_at = :t",
        ExpressionAttributeValues={":t": {"S": "2026-06-08T00:00:00Z"}},
    )
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is False
    mock_grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_backfill_dry_run_grants_nothing(tables):
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        summary = await run_backfill(env="test", apply=False, credits_table="isol8-test-credits")
    mock_grant.assert_not_awaited()
    # Dry-run still reports the funded owner as a candidate, skips the zero one.
    assert summary["candidates"] == 1
    assert summary["granted"] == 0


@pytest.mark.asyncio
async def test_run_backfill_apply_grants_only_positive_balances(tables):
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        summary = await run_backfill(env="test", apply=True, credits_table="isol8-test-credits")
    # Only org_funded granted; org_zero skipped.
    mock_grant.assert_awaited_once()
    assert summary["granted"] == 1
