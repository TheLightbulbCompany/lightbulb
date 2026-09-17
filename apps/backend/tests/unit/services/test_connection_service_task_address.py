"""The relay presence row carries the accepting backend task's address.

Spec §4.4 (M2 node exec): the desktop's WebSocket is pinned to whichever
backend task accepted the upgrade, so the presence row `put_relay_presence`
writes must record that task's own address, `refresh_relay_presence` must
re-assert it on every 60s tick, and `get_connection` must project it so a
peer task can read it back. `ConnectionService.find_node_presence` (M2-R3)
is the read Task 28's bridge calls directly to find where to forward a
node-invoke HTTP call.
"""

import time

import boto3
import pytest
from moto import mock_aws

from core.services import task_address
from core.services.connection_service import ConnectionService


def test_the_presence_row_records_the_accepting_tasks_address(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "put_item", lambda **kw: captured.update(kw))
    identity = type(
        "I",
        (),
        {
            "user_id": "u",
            "org_id": "o",
            "owner_id": "org_1",
            "kind": "node",
            "auth_kind": "device",
            "client_id": "node-host",
            "device_id": "dev-1",
        },
    )()
    import asyncio

    asyncio.run(service.put_relay_presence(connection_id="relay#1", identity=identity))
    assert captured["Item"]["taskAddress"] == {"S": "http://10.0.1.7:8000"}


def test_the_refresh_re_asserts_the_address_so_a_row_never_goes_addressless(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "update_item", lambda **kw: captured.update(kw))
    import asyncio

    asyncio.run(service.refresh_relay_presence("relay#1"))
    assert ":addr" in captured["ExpressionAttributeValues"]
    assert "taskAddress" in captured["UpdateExpression"]


def test_own_address_is_the_tasks_own_eni_ip(monkeypatch):
    task_address.own_address.cache_clear()
    monkeypatch.setattr(task_address.socket, "gethostbyname", lambda host: "10.0.2.9")
    assert task_address.own_address() == "http://10.0.2.9:8000"


# ---------------------------------------------------------------------------
# ConnectionService.find_node_presence (M2-R3) — Task 28's bridge calls this
# directly to learn which task accepted the owner's desktop node socket.
# ---------------------------------------------------------------------------


@pytest.fixture
def ddb_table_with_owner_gsi():
    """Same shape as test_connection_service_owner.py's fixture: an
    in-memory ws-connections table with the by-owner-id GSI."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="ws-conn-test-taskaddr",
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
        yield "ws-conn-test-taskaddr"


def _admit_node(
    table: str, connection_id: str, owner_id: str, device_id: str, task_addr: str, ttl: int | None = None
) -> None:
    item = {
        "connectionId": {"S": connection_id},
        "userId": {"S": "u1"},
        "orgId": {"S": owner_id},
        "ownerId": {"S": owner_id},
        "connectionType": {"S": "node"},
        "deviceId": {"S": device_id},
        "taskAddress": {"S": task_addr},
    }
    if ttl is not None:
        item["ttl"] = {"N": str(ttl)}
    boto3.client("dynamodb", region_name="us-east-1").put_item(TableName=table, Item=item)


@pytest.mark.asyncio
async def test_find_node_presence_returns_the_owners_live_node_row(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit_node(ddb_table_with_owner_gsi, "relay#1", "org_1", "dev-1", "http://10.0.1.7:8000")

    result = await svc.find_node_presence("org_1")

    assert result == {
        "connectionId": "relay#1",
        "taskAddress": "http://10.0.1.7:8000",
        "deviceId": "dev-1",
    }


@pytest.mark.asyncio
async def test_find_node_presence_returns_none_when_owner_has_no_node_row(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")

    assert await svc.find_node_presence("org_1") is None


@pytest.mark.asyncio
async def test_find_node_presence_returns_none_when_the_only_row_is_expired(ddb_table_with_owner_gsi):
    """DynamoDB's TTL sweep can lag up to 48h (see has_live_operator_connection),
    so a dead task's row must not be forwarded to -- same freshness rule."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit_node(
        ddb_table_with_owner_gsi, "relay#1", "org_1", "dev-1", "http://10.0.1.7:8000", ttl=int(time.time()) - 10
    )

    assert await svc.find_node_presence("org_1") is None


@pytest.mark.asyncio
async def test_find_node_presence_returns_a_fresh_ttl_row(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit_node(
        ddb_table_with_owner_gsi, "relay#1", "org_1", "dev-1", "http://10.0.1.7:8000", ttl=int(time.time()) + 120
    )

    assert await svc.find_node_presence("org_1") == {
        "connectionId": "relay#1",
        "taskAddress": "http://10.0.1.7:8000",
        "deviceId": "dev-1",
    }


@pytest.mark.asyncio
async def test_find_node_presence_treats_a_missing_ttl_as_live(ddb_table_with_owner_gsi):
    """Over-preserve, same bias as has_live_operator_connection: a row with
    no ttl attribute at all is treated as live."""
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    _admit_node(ddb_table_with_owner_gsi, "relay#1", "org_1", "dev-1", "http://10.0.1.7:8000", ttl=None)

    assert await svc.find_node_presence("org_1") is not None
