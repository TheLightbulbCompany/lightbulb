import os
from unittest.mock import MagicMock, patch

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


@pytest.mark.asyncio
async def test_put_stores_the_mint_time_in_milliseconds():
    table = MagicMock()
    with patch.object(repo, "_table", return_value=table), patch("time.time", return_value=1234.5678):
        await repo.put("st", owner_id="org_1", provider="google", verifier="v")
    assert table.put_item.call_args.kwargs["Item"]["created_at_ms"] == 1_234_567


@pytest.mark.parametrize(
    "rec,before",
    [
        ({"created_at_ms": 200_500, "ttl": 800}, True),  # at the mark
        ({"created_at_ms": 200_501, "ttl": 800}, False),  # the same second, after it
        ({"ttl": 200 + 600}, True),  # legacy: the mark's own second fails closed
        ({"ttl": 201 + 600}, False),
    ],
)
def test_started_before(rec, before):
    assert repo.started_before(rec, 200_500) is before
