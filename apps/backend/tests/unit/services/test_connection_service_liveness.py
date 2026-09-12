"""Unit tests for retained machine-connection liveness plumbing."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services.connection_service import ConnectionService


@pytest.fixture
def plain_ddb_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="ws-conn-test",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield "ws-conn-test"


@pytest.mark.asyncio
async def test_gateway_connected_fresh_and_absent(plain_ddb_table):
    service = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    await service.mark_gateway_connected("owner_1", conn_token="t1")

    assert await service.is_gateway_connected("owner_1") is True
    assert await service.is_gateway_connected("owner_absent") is False


@pytest.mark.asyncio
async def test_gateway_connected_stale_is_false(plain_ddb_table):
    from core.services.connection_service import _LIVENESS_STALE_SECONDS, _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    service = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    await service.mark_gateway_connected("owner_1", conn_token="t1")
    client.update_item(
        TableName=plain_ddb_table,
        Key={"connectionId": {"S": _gateway_liveness_key("owner_1")}},
        UpdateExpression="SET lastPingAt = :p",
        ExpressionAttributeValues={":p": {"N": str(int(time.time()) - _LIVENESS_STALE_SECONDS - 5)}},
    )

    assert await service.is_gateway_connected("owner_1") is False


@pytest.mark.asyncio
async def test_gateway_disconnect_only_deletes_own_token(plain_ddb_table):
    from core.services.connection_service import _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    service = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    key = _gateway_liveness_key("owner_1")
    await service.mark_gateway_connected("owner_1", conn_token="replacement")

    await service.mark_gateway_disconnected("owner_1", conn_token="old")
    assert "Item" in client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})

    await service.mark_gateway_disconnected("owner_1", conn_token="replacement")
    assert "Item" not in client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})


@pytest.mark.asyncio
async def test_gateway_refresh_recovers_missing_without_stealing_newer_token(plain_ddb_table):
    from core.services.connection_service import _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    service = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    key = _gateway_liveness_key("owner_1")

    await service.refresh_gateway_liveness("owner_1", conn_token="old")
    assert (
        client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["connToken"]["S"] == "old"
    )

    await service.mark_gateway_connected("owner_1", conn_token="new")
    await service.refresh_gateway_liveness("owner_1", conn_token="old")
    assert (
        client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["connToken"]["S"] == "new"
    )


@pytest.mark.asyncio
async def test_live_gateway_token_read_is_strongly_consistent():
    service = ConnectionService.__new__(ConnectionService)
    service.table_name = "tbl"
    service._client = MagicMock(
        get_item=MagicMock(
            return_value={
                "Item": {
                    "lastPingAt": {"N": str(int(time.time()))},
                    "connToken": {"S": "tok-live"},
                }
            }
        )
    )

    assert await service.get_live_gateway_conn_token("owner_1") == "tok-live"
    assert service._client.get_item.call_args.kwargs["ConsistentRead"] is True


@pytest.mark.asyncio
async def test_live_gateway_token_read_propagates_failures():
    service = ConnectionService.__new__(ConnectionService)
    service.table_name = "tbl"
    service._client = MagicMock()
    service._client.get_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}},
        "GetItem",
    )

    with pytest.raises(ClientError):
        await service.get_live_gateway_conn_token("owner_1")
