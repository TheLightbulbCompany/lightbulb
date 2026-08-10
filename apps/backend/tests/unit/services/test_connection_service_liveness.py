"""Unit tests for the liveness plumbing added on top of ConnectionService:

- ``store_connection`` stamping ``ttl`` + ``lastPingAt`` at connect.
- ``touch_connection`` refreshing both on each ping, throttled in-process.
- ``count_live_chat_connections`` filtering rules.

Mirrors the moto-backed conventions in test_connection_service_owner.py
(the ``by-owner-id`` GSI fixture) and the mocked-client conventions in
test_connection_service_errors.py (for throttle/error-swallow assertions
that need to inspect call counts rather than DDB state).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services.connection_service import (
    ConnectionService,
    _LIVE_WINDOW_SECONDS,
    _TOUCH_THROTTLE_MAX_ENTRIES,
)


# ---------------------------------------------------------------------------
# moto fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ddb_table_with_owner_gsi():
    """Same shape as test_connection_service_owner.py's fixture: base table
    plus the by-owner-id GSI with its real INCLUDE projection (userId,
    connectionType only -- NOT lastPingAt), so tests exercise the same
    "extra base-table read" path count_live_chat_connections relies on.
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


@pytest.fixture
def plain_ddb_table():
    """Base table only, no GSI -- enough for store_connection/get_item/
    touch_connection tests that don't query by owner."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="ws-conn-test",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield "ws-conn-test"


# ---------------------------------------------------------------------------
# store_connection stamps ttl + lastPingAt
# ---------------------------------------------------------------------------


def test_store_connection_writes_ttl_and_last_ping_at(plain_ddb_table):
    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")

    before = int(time.time())
    svc.store_connection(connection_id="c1", user_id="user_a", org_id=None, connection_type="chat")
    after = int(time.time())

    item = client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": "c1"}})["Item"]
    last_ping = int(item["lastPingAt"]["N"])
    ttl = int(item["ttl"]["N"])

    assert before <= last_ping <= after
    # ttl must be stamped comfortably in the future (2h grace window).
    assert ttl > after + 3600


# ---------------------------------------------------------------------------
# touch_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_connection_refreshes_last_ping_and_ttl(plain_ddb_table):
    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    svc.store_connection(connection_id="c1", user_id="user_a", org_id=None, connection_type="chat")

    # Seed stale values directly, bypassing store_connection's own stamp,
    # so we can prove touch_connection actually rewrote them.
    client.update_item(
        TableName=plain_ddb_table,
        Key={"connectionId": {"S": "c1"}},
        UpdateExpression="SET lastPingAt = :p, #ttl = :t",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":p": {"N": "1000"}, ":t": {"N": "2000"}},
    )

    await svc.touch_connection("c1")

    item = client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": "c1"}})["Item"]
    assert int(item["lastPingAt"]["N"]) > 1000
    assert int(item["ttl"]["N"]) > 2000


@pytest.mark.asyncio
async def test_touch_connection_missing_connection_is_a_noop(plain_ddb_table):
    """A ping racing a concurrent $disconnect must not resurrect a phantom
    row with only lastPingAt/ttl and none of the owner/user/type fields."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")

    await svc.touch_connection("ghost")

    response = client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": "ghost"}})
    assert "Item" not in response


@pytest.mark.asyncio
async def test_touch_connection_throttles_within_window():
    mock_ddb = MagicMock()
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    await svc.touch_connection("c1")
    await svc.touch_connection("c1")  # immediate re-touch, same id -- throttled

    assert mock_ddb.update_item.call_count == 1


@pytest.mark.asyncio
async def test_touch_connection_writes_again_after_throttle_window_elapses():
    mock_ddb = MagicMock()
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    await svc.touch_connection("c1")
    # Simulate the throttle window having elapsed without sleeping in a test.
    svc._touch_throttle["c1"] = time.time() - 61
    await svc.touch_connection("c1")

    assert mock_ddb.update_item.call_count == 2


@pytest.mark.asyncio
async def test_touch_connection_throttle_independent_per_connection():
    mock_ddb = MagicMock()
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    await svc.touch_connection("c1")
    await svc.touch_connection("c2")

    assert mock_ddb.update_item.call_count == 2


@pytest.mark.asyncio
async def test_touch_connection_swallows_ddb_errors():
    mock_ddb = MagicMock()
    mock_ddb.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem"
    )
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    # Must not raise -- this runs detached via asyncio.create_task in the
    # ping handler with nothing to catch it.
    await svc.touch_connection("c1")


@pytest.mark.asyncio
async def test_touch_connection_swallows_conditional_check_failure():
    mock_ddb = MagicMock()
    mock_ddb.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
        "UpdateItem",
    )
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    await svc.touch_connection("c1")


@pytest.mark.asyncio
async def test_touch_connection_throttle_dict_bounded():
    mock_ddb = MagicMock()
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        svc = ConnectionService(table_name="test-connections")

    # Pre-fill past the bound with old timestamps so a real deployment
    # scenario (many distinct connections touched over a long process
    # lifetime) is simulated without actually touching that many.
    old = time.time() - 1000
    svc._touch_throttle = {f"conn_{i}": old for i in range(_TOUCH_THROTTLE_MAX_ENTRIES)}

    await svc.touch_connection("new_conn")

    # The dict must have been cleared rather than left to grow -- only the
    # just-touched connection remains tracked.
    assert len(svc._touch_throttle) == 1
    assert "new_conn" in svc._touch_throttle


# ---------------------------------------------------------------------------
# count_live_chat_connections
# ---------------------------------------------------------------------------


def _seed_row(client, table, connection_id, owner_id, user_id, connection_type, last_ping_at=None):
    item = {
        "connectionId": {"S": connection_id},
        "userId": {"S": user_id},
        "orgId": {"S": ""},
        "ownerId": {"S": owner_id},
        "connectedAt": {"S": "2026-07-10T00:00:00+00:00"},
        "connectionType": {"S": connection_type},
        "ttl": {"N": str(int(time.time()) + 7200)},
    }
    if last_ping_at is not None:
        item["lastPingAt"] = {"N": str(last_ping_at)}
    client.put_item(TableName=table, Item=item)


@pytest.mark.asyncio
async def test_count_live_chat_connections_counts_fresh_chat_row(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    svc.store_connection(connection_id="c1", user_id="m1", org_id="org_x", connection_type="chat")

    count = await svc.count_live_chat_connections("org_x")
    assert count == 1


@pytest.mark.asyncio
async def test_count_live_chat_connections_excludes_node_and_operator_rows(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    svc.store_connection(connection_id="c1", user_id="m1", org_id="org_x", connection_type="chat")
    svc.store_connection(connection_id="c2", user_id="m1", org_id="org_x", connection_type="node")
    svc.store_connection(connection_id="c3", user_id="m1", org_id="org_x", connection_type="operator")

    count = await svc.count_live_chat_connections("org_x")
    assert count == 1


@pytest.mark.asyncio
async def test_count_live_chat_connections_excludes_stale_ping(ddb_table_with_owner_gsi):
    client = boto3.client("dynamodb", region_name="us-east-1")
    stale = int(time.time()) - (_LIVE_WINDOW_SECONDS + 30)
    _seed_row(client, ddb_table_with_owner_gsi, "c1", "org_x", "m1", "chat", last_ping_at=stale)

    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    count = await svc.count_live_chat_connections("org_x")
    assert count == 0


@pytest.mark.asyncio
async def test_count_live_chat_connections_includes_missing_last_ping_at(ddb_table_with_owner_gsi):
    """Fail-safe rule: a chat row with no lastPingAt at all (pre-deploy
    legacy row) counts as live rather than being silently excluded."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    _seed_row(client, ddb_table_with_owner_gsi, "c1", "org_x", "m1", "chat", last_ping_at=None)

    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    count = await svc.count_live_chat_connections("org_x")
    assert count == 1


@pytest.mark.asyncio
async def test_count_live_chat_connections_mixed_rows(ddb_table_with_owner_gsi):
    client = boto3.client("dynamodb", region_name="us-east-1")
    fresh = int(time.time())
    stale = int(time.time()) - (_LIVE_WINDOW_SECONDS + 30)

    _seed_row(client, ddb_table_with_owner_gsi, "c_fresh", "org_x", "m1", "chat", last_ping_at=fresh)
    _seed_row(client, ddb_table_with_owner_gsi, "c_stale", "org_x", "m2", "chat", last_ping_at=stale)
    _seed_row(client, ddb_table_with_owner_gsi, "c_missing", "org_x", "m3", "chat", last_ping_at=None)
    _seed_row(client, ddb_table_with_owner_gsi, "c_node", "org_x", "m1", "node", last_ping_at=fresh)

    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    count = await svc.count_live_chat_connections("org_x")
    assert count == 2  # c_fresh + c_missing; c_stale excluded, c_node excluded


@pytest.mark.asyncio
async def test_count_live_chat_connections_unknown_owner_returns_zero(ddb_table_with_owner_gsi):
    svc = ConnectionService(table_name=ddb_table_with_owner_gsi, region_name="us-east-1")
    count = await svc.count_live_chat_connections("nobody")
    assert count == 0


# ---------------------------------------------------------------------------
# Gateway-liveness lease: refresh while alive + freshness check on read
# (COMPOSER P2 — a one-shot ttl let a live long-lived connection's row get
# TTL-swept, and let an expired-but-unswept row read as connected).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_gateway_connected_true_when_fresh(plain_ddb_table):
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    await svc.mark_gateway_connected("owner_1", conn_token="t1")
    assert await svc.is_gateway_connected("owner_1") is True


@pytest.mark.asyncio
async def test_is_gateway_connected_false_when_stale(plain_ddb_table):
    """A row whose lastPingAt is older than the freshness window reads as NOT
    connected even though it still exists (TTL sweep lag / dead gateway whose
    refresh loop stopped) -- fail toward cold-start, never a false ready."""
    from core.services.connection_service import _LIVENESS_STALE_SECONDS, _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    await svc.mark_gateway_connected("owner_1", conn_token="t1")
    stale = int(time.time()) - _LIVENESS_STALE_SECONDS - 5
    client.update_item(
        TableName=plain_ddb_table,
        Key={"connectionId": {"S": _gateway_liveness_key("owner_1")}},
        UpdateExpression="SET lastPingAt = :p",
        ExpressionAttributeValues={":p": {"N": str(stale)}},
    )
    assert await svc.is_gateway_connected("owner_1") is False


@pytest.mark.asyncio
async def test_is_gateway_connected_false_when_absent(plain_ddb_table):
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    assert await svc.is_gateway_connected("owner_nope") is False


@pytest.mark.asyncio
async def test_mark_gateway_connected_is_an_upsert_that_recovers_a_missing_row(plain_ddb_table):
    """The refresh loop re-dispatches mark_gateway_connected (a full put), so a
    connection whose INITIAL connect put was lost (throttled / transient error)
    recovers on the next tick rather than reading `starting` forever. Verifies
    the put creates the row from absent AND bumps lastPingAt on a repeat."""
    from core.services.connection_service import _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    key = _gateway_liveness_key("owner_1")
    # Absent -> created.
    await svc.mark_gateway_connected("owner_1", conn_token="t1")
    first = int(client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["lastPingAt"]["N"])
    # Backdate then re-put (a refresh tick) -- lastPingAt jumps forward.
    client.update_item(
        TableName=plain_ddb_table,
        Key={"connectionId": {"S": key}},
        UpdateExpression="SET lastPingAt = :p",
        ExpressionAttributeValues={":p": {"N": str(first - 120)}},
    )
    await svc.mark_gateway_connected("owner_1", conn_token="t1")
    second = int(
        client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["lastPingAt"]["N"]
    )
    assert second >= first


@pytest.mark.asyncio
async def test_mark_gateway_disconnected_only_deletes_own_token(plain_ddb_table):
    """Replacement safety: when a stale connection is replaced (or backend
    instances overlap on a rolling deploy), the replacement writes the row with
    ITS token. The old connection's disconnect, conditioned on the old token,
    must NOT remove the replacement's live marker -- otherwise a healthy gateway
    reads `starting` until the next refresh."""
    from core.services.connection_service import _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    key = _gateway_liveness_key("owner_1")

    # The replacement connection owns the row now (token t2).
    await svc.mark_gateway_connected("owner_1", conn_token="t2")
    # The OLD connection (token t1) disconnects -- conditional no-op.
    await svc.mark_gateway_disconnected("owner_1", conn_token="t1")
    assert "Item" in client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})

    # The owning connection's own disconnect DOES clear it.
    await svc.mark_gateway_disconnected("owner_1", conn_token="t2")
    assert "Item" not in client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})


@pytest.mark.asyncio
async def test_refresh_recovers_missing_but_never_steals_a_newer_token(plain_ddb_table):
    """The refresh loop uses the CONDITIONAL refresh (absent OR own token), not
    the unconditional claim. So a lost initial connect recovers, but a
    stale-but-alive connection's refresh can't overwrite a replacement's newer
    token (which its own token-conditioned delete would then wrongly clear)."""
    from core.services.connection_service import _gateway_liveness_key

    client = boto3.client("dynamodb", region_name="us-east-1")
    svc = ConnectionService(table_name=plain_ddb_table, region_name="us-east-1")
    key = _gateway_liveness_key("owner_1")

    # Missing row (initial connect put was lost): refresh recreates it as ours.
    await svc.refresh_gateway_liveness("owner_1", conn_token="t1")
    assert (
        client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["connToken"]["S"] == "t1"
    )

    # A replacement claims the row with a newer token.
    await svc.mark_gateway_connected("owner_1", conn_token="t2")
    # The OLD connection's refresh (t1) must be a conditional no-op.
    await svc.refresh_gateway_liveness("owner_1", conn_token="t1")
    assert (
        client.get_item(TableName=plain_ddb_table, Key={"connectionId": {"S": key}})["Item"]["connToken"]["S"] == "t2"
    )


# ---------------------------------------------------------------------------
# get_live_gateway_conn_token — the grace-path stop veto's read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_live_gateway_conn_token_uses_a_strongly_consistent_read():
    """The token read gates an irreversible container stop: a replacement's
    mark_gateway_connected may have landed milliseconds ago on another
    worker, so the read must be strongly consistent — and it must surface
    the row's token when fresh."""
    svc = ConnectionService.__new__(ConnectionService)
    svc.table_name = "tbl"
    client = MagicMock()
    client.get_item.return_value = {
        "Item": {
            "lastPingAt": {"N": str(int(time.time()))},
            "connToken": {"S": "tok-live"},
        }
    }
    svc._client = client

    token = await svc.get_live_gateway_conn_token("owner_1")

    assert token == "tok-live"
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True


@pytest.mark.asyncio
async def test_get_live_gateway_conn_token_propagates_read_failures():
    """Unlike is_gateway_connected (fails toward the cold-start UI), the
    stop veto's read must NOT map failure to \"no replacement\" — it raises,
    and the stop path's catch-all aborts that stop attempt."""
    svc = ConnectionService.__new__(ConnectionService)
    svc.table_name = "tbl"
    client = MagicMock()
    client.get_item.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "GetItem")
    svc._client = client

    with pytest.raises(ClientError):
        await svc.get_live_gateway_conn_token("owner_1")
