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

Note: Table creation is handled by Terraform, not this service.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


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
                       WS_CONNECTIONS_TABLE env var (default: "isol8-websocket-connections")
            region_name: AWS region. If not provided, uses AWS_REGION env var.
        """
        self.table_name = table_name or os.environ.get("WS_CONNECTIONS_TABLE", "isol8-websocket-connections")
        # Explicitly set region to avoid NoRegionError in containerized environments
        region = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._client = boto3.client("dynamodb", region_name=region)

    def store_connection(
        self,
        connection_id: str,
        user_id: str,
        org_id: Optional[str],
        connection_type: str = "chat",
    ) -> None:
        """
        Store a new WebSocket connection mapping.

        Called on $connect when a user establishes a WebSocket connection.

        Args:
            connection_id: API Gateway WebSocket connection ID
            user_id: Authenticated user's ID from Clerk JWT
            org_id: Organization ID from Clerk JWT (None for personal context)
            connection_type: "chat" (default) or "node" for desktop node-host connections

        Raises:
            ConnectionServiceError: If DynamoDB operation fails
        """
        connected_at = datetime.now(timezone.utc).isoformat()

        item = {
            "connectionId": {"S": connection_id},
            "userId": {"S": user_id},
            "orgId": {"S": org_id or ""},  # Empty string for None
            "ownerId": {"S": (org_id or user_id)},  # owner = org for org members, else the user
            "connectedAt": {"S": connected_at},
            "connectionType": {"S": connection_type},
        }

        try:
            self._client.put_item(
                TableName=self.table_name,
                Item=item,
            )
            logger.info(
                "Stored connection %s for user %s (org: %s)",
                connection_id,
                user_id,
                org_id or "personal",
            )
        except ClientError as e:
            logger.error(
                "Failed to store connection %s: %s",
                connection_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to store connection {connection_id}: {e.response['Error']['Message']}"
            ) from e

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

        if "Item" not in response:
            logger.debug("Connection %s not found", connection_id)
            return None

        item = response["Item"]
        org_id_value = item["orgId"]["S"]

        return {
            "user_id": item["userId"]["S"],
            "org_id": org_id_value if org_id_value else None,  # Convert empty string to None
            "connection_type": item.get("connectionType", {}).get("S", "chat"),
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

    async def query_by_user_id(self, user_id: str) -> list[str]:
        """Return every active connection ID owned by ``user_id``.

        Uses the ``by-user-id`` GSI added in
        ``apps/infra/lib/stacks/api-stack.ts``. KEYS_ONLY projection means
        only ``connectionId`` is returned per row — sufficient for the
        TeamsEventBroker fanout. Paginates ``LastEvaluatedKey`` because a
        single Query page caps at ~1MB; in practice one page suffices
        (a typical user has 1-3 live tabs) but pagination keeps the
        contract correct under load.

        Returns an empty list if the user has no live connections.
        """

        def _query() -> list[str]:
            paginator = self._client.get_paginator("query")
            pages = paginator.paginate(
                TableName=self.table_name,
                IndexName="by-user-id",
                KeyConditionExpression="userId = :u",
                ExpressionAttributeValues={":u": {"S": user_id}},
                ProjectionExpression="connectionId",
            )
            ids: list[str] = []
            for page in pages:
                for item in page.get("Items", []):
                    ids.append(item["connectionId"]["S"])
            return ids

        try:
            return await asyncio.to_thread(_query)
        except ClientError as e:
            logger.error(
                "Failed to query connections for user %s: %s",
                user_id,
                e.response["Error"]["Message"],
            )
            raise ConnectionServiceError(
                f"Failed to query connections for user {user_id}: {e.response['Error']['Message']}"
            ) from e

    async def query_by_owner(self, owner_id: str) -> list[tuple[str, str, str]]:
        """Return ``(connectionId, member_userId, connectionType)`` for every
        active connection owned by ``owner_id``.

        ``owner_id`` is the org id for org members and the user id for personal
        accounts (written by ``store_connection`` as ``ownerId``). Uses the
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
