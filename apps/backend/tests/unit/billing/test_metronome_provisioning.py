"""Unit tests for Metronome customer provisioning (mocked client + moto DDB)."""

from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_provisioning import ensure_metronome_customer


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


def _fake_client(customer_id="cust-uuid-1"):
    fake = MagicMock()
    fake.create_customer = AsyncMock(return_value=customer_id)
    fake.set_stripe_billing_config = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_creates_customer_with_ingest_alias_and_links_stripe(fast_gate_table):
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(
        owner_id="org_abc",
        stripe_customer_id="cus_123",
        client=fake,
    )
    assert cust_id == "cust-uuid-1"
    fake.create_customer.assert_awaited_once_with(name="org_abc", ingest_aliases=["org_abc"])
    fake.set_stripe_billing_config.assert_awaited_once_with(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )
    # Persisted for idempotency.
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["metronome_customer_id"]["S"] == "cust-uuid-1"


@pytest.mark.asyncio
async def test_idempotent_skips_http_when_already_provisioned(fast_gate_table):
    fast_gate_table.put_item(
        TableName="test-metronome-fast-gate",
        Item={"owner_id": {"S": "org_abc"}, "metronome_customer_id": {"S": "existing-uuid"}},
    )
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(
        owner_id="org_abc",
        stripe_customer_id="cus_123",
        client=fake,
    )
    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.set_stripe_billing_config.assert_not_awaited()
