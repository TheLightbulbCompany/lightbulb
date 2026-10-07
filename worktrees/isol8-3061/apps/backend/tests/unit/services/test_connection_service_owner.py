import time

import boto3
import pytest
from moto import mock_aws

from core.services.connection_service import ConnectionService


@pytest.fixture
def ddb_table_with_owner_gsi():
    """Stand up an in-memory ws-connections table with the by-owner-id GSI.

    Mirrors the real index (``apps/infra/lib/stacks/api-stack.ts:499``): an
    INCLUDE projection carrying userId + connectionType, so fan-out can demux
    per member and filter by connection type.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="ws-conn-test",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "connectionId", "AttributeType": "S"},
                {"AttributeName": "ownerId", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner-id",
                    "KeySchema": [{"AttributeName": "ownerId", "KeyType": "HASH"}],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": ["userId", "connectionType"],
                    },
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield "ws-conn-test"


def _admit(table: str, connection_id: str, user_id: str, org_id: str | None, connection_type: str) -> None:
    """Write an admission row the way the ``ws-connect`` Lambda does.

    That Lambda (``apps/infra/lambda/ws-connect/index.py``) is the only writer
    of this table, so these tests stand rows up the same way rather than through
    the service, which only reads them.
    """
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName=table,
        Item={
            "connectionId": {"S": connection_id},
            "userId": {"S": user_id},
            "orgId": {"S": org_id or ""},
            "ownerId": {"S": org_id or user_id},
            "connectionType": {"S": connection_type},
        },
    )


@pytest.mark.asyncio
async def test_query_by_owner_returns_members_and_types(ddb_table_with_owner_gsi):
    """An org's connections come back tagged with member userId + connectionType,
    including a non-chat (node) row."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "chat")
    _admit(ddb_table_with_owner_gsi, "c2", "m2", "org_x", "chat")
    _admit(ddb_table_with_owner_gsi, "c3", "m1", "org_x", "node")

    rows = await svc.query_by_owner("org_x")
    got = {(c, u, t) for (c, u, t) in rows}
    assert got == {
        ("c1", "m1", "chat"),
        ("c2", "m2", "chat"),
        ("c3", "m1", "node"),
    }


@pytest.mark.asyncio
async def test_query_by_owner_unknown_returns_empty(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "chat")

    assert await svc.query_by_owner("nobody") == []


@pytest.mark.asyncio
async def test_query_by_owner_personal_owner(ddb_table_with_owner_gsi):
    """Personal owner (ownerId == userId) resolves via the same GSI."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "p1", "user_p", None, "chat")

    rows = await svc.query_by_owner("user_p")
    assert rows == [("p1", "user_p", "chat")]


def _set_ttl(table: str, connection_id: str, ttl: int) -> None:
    boto3.client("dynamodb", region_name="us-east-1").update_item(
        TableName=table,
        Key={"connectionId": {"S": connection_id}},
        UpdateExpression="SET #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": {"N": str(ttl)}},
    )


@pytest.mark.asyncio
async def test_has_live_operator_connection_ignores_a_stale_ttl_row(ddb_table_with_owner_gsi):
    """A crashed instance's operator presence row must not veto trial-idle
    stop for the DynamoDB TTL sweep lag — only a row whose ttl is still in
    the future counts as live (ruling D4)."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "operator")
    _set_ttl(ddb_table_with_owner_gsi, "c1", int(time.time()) - 10)

    assert await svc.has_live_operator_connection("org_x") is False


@pytest.mark.asyncio
async def test_has_live_operator_connection_true_for_a_fresh_ttl_row(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "operator")
    _set_ttl(ddb_table_with_owner_gsi, "c1", int(time.time()) + 120)

    assert await svc.has_live_operator_connection("org_x") is True


@pytest.mark.asyncio
async def test_has_live_operator_connection_treats_a_missing_ttl_as_live(ddb_table_with_owner_gsi):
    """Over-preserve when a row has no ttl at all (defensive: never silently
    drop a real operator connection because of an absent attribute)."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "operator")

    assert await svc.has_live_operator_connection("org_x") is True


@pytest.mark.asyncio
async def test_has_live_operator_connection_false_when_no_operator_rows(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "chat")

    assert await svc.has_live_operator_connection("org_x") is False


def _set_device_id(table: str, connection_id: str, device_id: str) -> None:
    boto3.client("dynamodb", region_name="us-east-1").update_item(
        TableName=table,
        Key={"connectionId": {"S": connection_id}},
        UpdateExpression="SET deviceId = :d",
        ExpressionAttributeValues={":d": {"S": device_id}},
    )


@pytest.mark.asyncio
async def test_delete_relay_rows_for_device_deletes_only_matching_node_rows(ddb_table_with_owner_gsi):
    """The by-owner-id GSI has no deviceId projection, so this must fetch each
    node row in full to check it -- and never touch a non-node row or another
    device's row, even under the same owner."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "node")
    _set_device_id(ddb_table_with_owner_gsi, "c1", "dev_revoked")
    _admit(ddb_table_with_owner_gsi, "c2", "m1", "org_x", "node")
    _set_device_id(ddb_table_with_owner_gsi, "c2", "dev_other")
    _admit(ddb_table_with_owner_gsi, "c3", "m1", "org_x", "operator")  # not a node row

    deleted = await svc.delete_relay_rows_for_device("org_x", "dev_revoked")

    assert deleted == 1
    assert svc.get_connection("c1") is None
    assert svc.get_connection("c2") is not None
    assert svc.get_connection("c3") is not None


@pytest.mark.asyncio
async def test_delete_relay_rows_for_device_no_match_deletes_nothing(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit(ddb_table_with_owner_gsi, "c1", "m1", "org_x", "node")
    _set_device_id(ddb_table_with_owner_gsi, "c1", "dev_other")

    assert await svc.delete_relay_rows_for_device("org_x", "dev_revoked") == 0
    assert svc.get_connection("c1") is not None
