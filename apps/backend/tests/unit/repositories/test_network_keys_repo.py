"""network_keys_repo: computer key -> Lightbulb user, and a workspace's members' keys."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def keys_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-network-keys",
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "key", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-user",
                    "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield


@pytest.mark.asyncio
async def test_bind_rebind_list_and_unbind(keys_table):
    from core.repositories import network_keys_repo as repo

    assert await repo.put("k1", "user_a", "mac") is None
    assert await repo.put("k2", "user_a", "mbp") is None
    assert await repo.put("k3", "user_b", "pc") is None
    assert await repo.keys_for_users(["user_a", "user_b", "user_c"]) == ["k1", "k2", "k3"]
    assert await repo.keys_for_users([]) == []

    old = await repo.put("k2", "user_b", "mbp")  # someone else signs in on that computer
    assert old["user_id"] == "user_a"
    assert await repo.keys_for_users(["user_a"]) == ["k1"]
    assert (await repo.get("k2"))["user_id"] == "user_b"

    assert (await repo.delete("k1"))["user_id"] == "user_a"
    assert await repo.delete("k1") is None
    assert await repo.keys_for_users(["user_a"]) == []
