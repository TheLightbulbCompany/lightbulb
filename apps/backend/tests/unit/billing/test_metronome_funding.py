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
