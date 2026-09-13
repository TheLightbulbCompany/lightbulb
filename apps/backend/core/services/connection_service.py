"""
Connection Service - manages WebSocket connection state in DynamoDB.

This service stores the mapping between API Gateway WebSocket connectionIds
and authenticated user identities. The API Gateway Management API only knows
connectionId, so we need to track user_id and org_id separately.

Architecture:
- INBOUND: Client WebSocket -> API Gateway -> HTTP POST -> VPC Link -> ALB -> EC2
- OUTBOUND: EC2 -> POST to Management API -> API Gateway -> Client WebSocket

DynamoDB Table Schema:
- connectionId (S) - partition key
- userId (S) - the authenticated user's ID
- orgId (S) - organization ID (empty string if None)
- connectedAt (S) - ISO timestamp of connection
- ttl (N) - epoch seconds; DynamoDB native TTL sweep attribute
  (timeToLiveAttribute, api-stack.ts:277) for missed disconnect cleanup.

Note: Table creation is handled by Terraform, not this service.
"""

import asyncio
import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# TTL grace period stamped on every connect + ping refresh: how long a row
# survives past its last liveness stamp before DynamoDB's TTL sweep deletes
# it if $disconnect is never called (crashed tab, dropped connection, etc).
_TTL_WINDOW_SECONDS = 7200  # 2 hours


class ConnectionServiceError(Exception):
    """Base exception for connection service errors."""

    pass


class ConnectionService:
    """
    Service for managing WebSocket connection state in DynamoDB.

    Stores the mapping between API Gateway connectionId and user identity,
    enabling the backend to know which user a WebSocket connection belongs to.
    """

    def __init__(self, table_name: Optional[str] = None, region_name: Optional[str] = None):
        """
        Initialize the connection service.

        Args:
            table_name: DynamoDB table name. If not provided, uses
                       WS_CONNECTIONS_TABLE env var (default: "lightbulb-dev-ws-connections")
            region_name: AWS region. If not provided, uses AWS_REGION env var.
        """
        self.table_name = table_name or os.environ.get("WS_CONNECTIONS_TABLE", "lightbulb-dev-ws-connections")
        # Explicitly set region to avoid NoRegionError in containerized environments
        region = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._client = boto3.client("dynamodb", region_name=region)

    def get_connection(self, connection_id: str) -> Optional[dict]:
        """
        Get the user identity for a WebSocket connection.

        Args:
            connection_id: API Gateway WebSocket connection ID

        Returns:
            Dict with user_id and org_id (org_id is None for personal context),
            or None if connection not found

        Raises:
            ConnectionServiceError: If DynamoDB operation fails
        """
        try:
            response = self._client.get_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": connection_id}},
                ConsistentRead=True,
            )
        except ClientError as e:
            logger.error(
                "Failed to get connection %s: %s",
                connection_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to get connection {connection_id}: {e.response['Error']['Message']}"
            ) from e

        if "Item" not in response or "userId" not in response["Item"]:
            logger.debug("Connection %s not found", connection_id)
            return None

        item = response["Item"]
        org_id_value = item["orgId"]["S"]

        return {
            "user_id": item["userId"]["S"],
            "org_id": org_id_value if org_id_value else None,  # Convert empty string to None
            "connection_type": item.get("connectionType", {}).get("S", "chat"),
            **{
                name: item.get(field, {}).get("S", "")
                for name, field in (
                    ("auth_kind", "authKind"),
                    ("client_id", "clientId"),
                    ("connected_at", "connectedAt"),
                    ("verified_issuer", "verifiedIssuer"),
                    ("verified_subject", "verifiedSubject"),
                    ("source_ip", "sourceIp"),
                    ("device_id", "deviceId"),
                )
                if field in item
            },
        }

    async def count_for_user(self, user_id: str) -> int:
        """Count WS connection rows owned by ``user_id``.

        Used by the e2e ``/debug/ddb-rows`` verification endpoint. Same
        rationale as ``delete_all_for_user``: no GSI on ``userId``, so we
        paginate-scan with a filter expression. Acceptable for a debug-only
        read because the table is small (one row per live connection, TTL
        purges stale rows) and any one user has at most a handful of rows.
        """

        def _scan_count() -> int:
            paginator = self._client.get_paginator("scan")
            pages = paginator.paginate(
                TableName=self.table_name,
                FilterExpression="userId = :u",
                ExpressionAttributeValues={":u": {"S": user_id}},
                Select="COUNT",
            )
            count = 0
            for page in pages:
                count += int(page.get("Count", 0))
            return count

        try:
            return await asyncio.to_thread(_scan_count)
        except ClientError as e:
            logger.error(
                "Failed to count connections for user %s: %s",
                user_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to count connections for user {user_id}: {e.response['Error']['Message']}"
            ) from e

    async def query_by_owner(self, owner_id: str) -> list[tuple[str, str, str]]:
        """Return ``(connectionId, member_userId, connectionType)`` for every
        active connection owned by ``owner_id``.

        ``owner_id`` is the org id for org members and the user id for personal
        accounts (written by the ``ws-connect`` Lambda as ``ownerId`` —
        ``apps/infra/lambda/ws-connect/index.py``, the only writer of this
        table's admission rows). Uses the
        ``by-owner-id`` GSI added in ``apps/infra/lib/stacks/api-stack.ts``,
        whose INCLUDE projection carries ``userId`` + ``connectionType`` so the
        gateway fan-out can demux per member and filter by connection type.
        Paginates ``LastEvaluatedKey`` so the contract stays correct when an
        org has more live connections than fit in one ~1MB Query page.

        Returns an empty list if the owner has no live connections.
        """

        def _query() -> list[tuple[str, str, str]]:
            paginator = self._client.get_paginator("query")
            pages = paginator.paginate(
                TableName=self.table_name,
                IndexName="by-owner-id",
                KeyConditionExpression="ownerId = :o",
                ExpressionAttributeValues={":o": {"S": owner_id}},
                ProjectionExpression="connectionId, userId, connectionType",
            )
            out: list[tuple[str, str, str]] = []
            for page in pages:
                for item in page.get("Items", []):
                    out.append(
                        (
                            item["connectionId"]["S"],
                            item.get("userId", {}).get("S", ""),
                            item.get("connectionType", {}).get("S", "chat"),
                        )
                    )
            return out

        try:
            return await asyncio.to_thread(_query)
        except ClientError as e:
            logger.error(
                "Failed to query connections for owner %s: %s",
                owner_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to query connections for owner {owner_id}: {e.response['Error']['Message']}"
            ) from e

    async def has_live_operator_connection(self, owner_id: str) -> bool:
        """Is a Paperclip run executing against this owner's container right now?

        The openclaw_gateway adapter opens its WS in ``execute()``, runs the
        turn, and closes — so an ``operator``-typed connection exists EXACTLY
        while a Paperclip-initiated run is in flight. That makes this a direct
        read of "is Paperclip using the container", with no run-state tracking
        of our own to keep in sync.

        Scale-to-zero needs it because Paperclip's operator socket is separate
        from backend-to-OpenClaw room run tracking. Stopping under it kills a
        legitimate turn, violating the stop path's over-preserve rule.

        No freshness check, unlike the chat count: an operator row is written at
        ``$connect`` and deleted at ``$disconnect``, and the adapter does not
        ping. Its mere existence is the signal.
        """
        rows = await self.query_by_owner(owner_id)
        return any(connection_type == "operator" for _connection_id, _user_id, connection_type in rows)

    async def delete_all_for_user(self, user_id: str) -> int:
        """Delete every WS connection row owned by ``user_id``.

        Used by the e2e teardown endpoint. The ws-connections table has no
        GSI on ``userId`` (only ``connectionId`` PK), so we scan with a
        filter expression. The table is small in practice (one row per live
        WS connection, with TTL purging stale rows) — full-table scans here
        are acceptable for a debug-only endpoint, and there is realistically
        at most a handful of rows for any one user.

        Returns:
            The number of rows deleted.
        """

        def _scan_and_delete() -> int:
            paginator = self._client.get_paginator("scan")
            pages = paginator.paginate(
                TableName=self.table_name,
                FilterExpression="userId = :u",
                ExpressionAttributeValues={":u": {"S": user_id}},
                ProjectionExpression="connectionId",
            )
            count = 0
            for page in pages:
                for item in page.get("Items", []):
                    self._client.delete_item(
                        TableName=self.table_name,
                        Key={"connectionId": {"S": item["connectionId"]["S"]}},
                    )
                    count += 1
            return count

        try:
            return await asyncio.to_thread(_scan_and_delete)
        except ClientError as e:
            logger.error(
                "Failed to delete connections for user %s: %s",
                user_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to delete connections for user {user_id}: {e.response['Error']['Message']}"
            ) from e

    def delete_connection(self, connection_id: str) -> None:
        """
        Delete a WebSocket connection mapping.

        Called on $disconnect when a user's WebSocket connection is closed.

        Args:
            connection_id: API Gateway WebSocket connection ID

        Raises:
            ConnectionServiceError: If DynamoDB operation fails

        Note:
            This is a no-op if the connection doesn't exist (DynamoDB behavior).
        """
        try:
            self._client.delete_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": connection_id}},
            )
            logger.info("Deleted connection %s", connection_id)
        except ClientError as e:
            logger.error(
                "Failed to delete connection %s: %s",
                connection_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to delete connection {connection_id}: {e.response['Error']['Message']}"
            ) from e


# ---------------------------------------------------------------------------
# Module-level singleton + thin async wrappers
#
# The router half of the codebase already owns a singleton via
# routers/websocket_chat.py::get_connection_service(). The debug teardown
# endpoint imports the *module* (``from core.services import connection_service``)
# and calls module-level helpers so it doesn't have to instantiate or share
# the router-side singleton. The helpers below give us that surface.
# ---------------------------------------------------------------------------

_singleton: Optional[ConnectionService] = None


def _get_singleton() -> ConnectionService:
    global _singleton
    if _singleton is None:
        _singleton = ConnectionService()
    return _singleton


async def delete_all_for_user(user_id: str) -> int:
    """Module-level helper: delete every WS connection row for ``user_id``."""
    return await _get_singleton().delete_all_for_user(user_id)


async def count_for_user(user_id: str) -> int:
    """Module-level helper: count every WS connection row for ``user_id``."""
    return await _get_singleton().count_for_user(user_id)
