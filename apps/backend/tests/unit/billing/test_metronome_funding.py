"""Unit tests for Metronome payment-gated funding (mocked client + moto DDB)."""

from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_funding import FundingError, fund_top_up


@pytest.fixture
def provisioned_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="test-metronome-fast-gate",
            Item={"owner_id": {"S": "org_abc"}, "metronome_customer_id": {"S": "cust-uuid-1"}},
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


def _fake_client():
    fake = MagicMock()
    fake.edit_contract_add_commits = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_fund_adds_payment_gated_stripe_commit(provisioned_table):
    # Pre-arm the cache as blocked so we can prove the top-up re-arms it.
    provisioned_table.update_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
        UpdateExpression="SET remaining_microcents = :z",
        ExpressionAttributeValues={":z": {"N": "0"}},
    )
    fake = _fake_client()
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,  # $25.00
        contract_id="contract-1",
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()
    _, kwargs = fake.edit_contract_add_commits.call_args
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    commit = kwargs["add_commits"][0]
    assert commit["product_id"] == "prod-credits"
    assert commit["amount"] == 25.0  # dollars
    assert commit["payment_gate_config"] == {"payment_gate_type": "STRIPE"}
    assert commit["metadata"]["stripe_session_id"] == "cs_test_123"
    # The cache is optimistically re-armed so the owner's next chat message
    # unblocks without waiting for a Metronome balance webhook (which won't
    # fire on a top-up). get_balance reads this cache, so without the re-arm a
    # paid top-up would never unblock chat.
    item = provisioned_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["remaining_microcents"]["N"] == "25000000"


@pytest.mark.asyncio
async def test_fund_rearm_failure_does_not_raise(provisioned_table, monkeypatch):
    # The Metronome commit is what releases money; the cache re-arm is a
    # best-effort convenience. A re-arm failure after a successful commit must
    # not raise (the next low-balance webhook re-syncs the authoritative
    # number), or the webhook would 500 back to Stripe AFTER the commit landed.
    fake = _fake_client()
    monkeypatch.setattr(
        "core.billing.metronome_funding.MetronomeFastGate.add_credit",
        AsyncMock(side_effect=RuntimeError("ddb down")),
    )
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,
        contract_id="contract-1",
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()


@pytest.mark.asyncio
async def test_fund_raises_when_customer_not_provisioned(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="not provisioned"):
        await fund_top_up(
            owner_id="org_unprovisioned",
            amount_microcents=25_000_000,
            contract_id="contract-1",
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )
    fake.edit_contract_add_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_fund_rejects_non_positive_amount(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await fund_top_up(
            owner_id="org_abc",
            amount_microcents=0,
            contract_id="contract-1",
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )


from core.billing.metronome_funding import set_auto_recharge  # noqa: E402


def _fake_client_recharge():
    fake = MagicMock()
    fake.set_credit_balance_threshold = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_set_auto_recharge_enable_maps_cents_to_target_balance(provisioned_table):
    fake = _fake_client_recharge()
    await set_auto_recharge(
        owner_id="org_abc",
        enabled=True,
        threshold_cents=500,  # $5.00 threshold
        amount_cents=2000,  # $20.00 recharge increment
        contract_id="contract-1",
        client=fake,
    )
    fake.set_credit_balance_threshold.assert_awaited_once()
    _, kwargs = fake.set_credit_balance_threshold.call_args
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["is_enabled"] is True
    assert kwargs["threshold_amount"] == 5.0
    # recharge_to = threshold ($5) + increment ($20) = $25 target balance.
    assert kwargs["recharge_to_amount"] == 25.0


@pytest.mark.asyncio
async def test_set_auto_recharge_disable_sends_only_is_enabled(provisioned_table):
    fake = _fake_client_recharge()
    await set_auto_recharge(owner_id="org_abc", enabled=False, contract_id="contract-1", client=fake)
    _, kwargs = fake.set_credit_balance_threshold.call_args
    assert kwargs["is_enabled"] is False
    assert kwargs.get("threshold_amount") is None
    assert kwargs.get("recharge_to_amount") is None


@pytest.mark.asyncio
async def test_set_auto_recharge_enable_requires_amounts(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="required when enabling"):
        await set_auto_recharge(owner_id="org_abc", enabled=True, contract_id="contract-1", client=fake)
    fake.set_credit_balance_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_auto_recharge_enforces_threshold_minimum(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="at least"):
        await set_auto_recharge(
            owner_id="org_abc",
            enabled=True,
            threshold_cents=100,  # $1 — below the $5 minimum
            amount_cents=2000,
            contract_id="contract-1",
            client=fake,
        )
    fake.set_credit_balance_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_auto_recharge_raises_when_customer_not_provisioned(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="not provisioned"):
        await set_auto_recharge(
            owner_id="org_unprovisioned",
            enabled=True,
            threshold_cents=500,
            amount_cents=2000,
            contract_id="contract-1",
            client=fake,
        )


from core.billing.metronome_funding import fund_backfill_credit  # noqa: E402


@pytest.mark.asyncio
async def test_fund_backfill_credit_is_not_payment_gated(provisioned_table):
    fake = _fake_client()
    await fund_backfill_credit(
        owner_id="org_abc",
        amount_microcents=12_000_000,  # $12.00
        contract_id="contract-1",
        product_id="prod-credits",
        client=fake,
    )
    _, kwargs = fake.edit_contract_add_commits.call_args
    commit = kwargs["add_commits"][0]
    assert commit["amount"] == 12.0
    # NON-payment-gated: the owner already paid in the old system.
    assert "payment_gate_config" not in commit
    assert commit["metadata"]["backfill_key"] == "ddb-balance-backfill:org_abc"


@pytest.mark.asyncio
async def test_fund_backfill_credit_rejects_non_positive(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await fund_backfill_credit(
            owner_id="org_abc",
            amount_microcents=0,
            contract_id="contract-1",
            product_id="prod-credits",
            client=fake,
        )
