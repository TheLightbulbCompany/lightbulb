import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import oauth_state_repo as repo  # noqa: E402


@pytest.fixture
def state_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-oauth-state",
            KeySchema=[{"AttributeName": "state", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "state", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-oauth-state")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_extra_fields_round_trip_and_state_is_single_use(state_table):
    await repo.put(
        "st_1", owner_id="org_1", provider="slack", verifier="", extra={"agent_id": "agent_f", "member_id": "user_1"}
    )
    rec = await repo.consume("st_1")
    assert rec["owner_id"] == "org_1"
    assert rec["agent_id"] == "agent_f"
    assert rec["member_id"] == "user_1"
    assert await repo.consume("st_1") is None
