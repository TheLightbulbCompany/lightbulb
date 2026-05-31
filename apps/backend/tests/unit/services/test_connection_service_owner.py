import boto3
import pytest
from moto import mock_aws

from core.services.connection_service import ConnectionService


@pytest.fixture
def ddb_table_with_owner_gsi():
    """Stand up an in-memory ws-connections table with the by-owner-id GSI.

    Mirrors the by-user-id GSI fixture in test_connection_service_query.py but
    uses an INCLUDE projection (userId + connectionType) so fan-out can demux
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


@mock_aws
def test_store_connection_writes_owner_id_org():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="ws-conn-test",
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    svc = ConnectionService(table_name="ws-conn-test", region_name="us-east-1")
    # org member: ownerId must be the org id
    svc.store_connection(connection_id="c1", user_id="user_a", org_id="org_x", connection_type="chat")
    item = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c1"}})["Item"]
    assert item["ownerId"]["S"] == "org_x"
    # personal: ownerId == userId
    svc.store_connection(connection_id="c2", user_id="user_b", org_id=None, connection_type="chat")
    item2 = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c2"}})["Item"]
    assert item2["ownerId"]["S"] == "user_b"


@pytest.mark.asyncio
async def test_query_by_owner_returns_members_and_types(ddb_table_with_owner_gsi):
    """An org's connections come back tagged with member userId + connectionType,
    including a non-chat (node) row."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    svc.store_connection(connection_id="c1", user_id="m1", org_id="org_x", connection_type="chat")
    svc.store_connection(connection_id="c2", user_id="m2", org_id="org_x", connection_type="chat")
    svc.store_connection(connection_id="c3", user_id="m1", org_id="org_x", connection_type="node")

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
    svc.store_connection(connection_id="c1", user_id="m1", org_id="org_x", connection_type="chat")

    assert await svc.query_by_owner("nobody") == []


@pytest.mark.asyncio
async def test_query_by_owner_personal_owner(ddb_table_with_owner_gsi):
    """Personal owner (ownerId == userId) resolves via the same GSI."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    svc.store_connection(connection_id="p1", user_id="user_p", org_id=None, connection_type="chat")

    rows = await svc.query_by_owner("user_p")
    assert rows == [("p1", "user_p", "chat")]
