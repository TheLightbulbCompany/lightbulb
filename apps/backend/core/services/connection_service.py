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
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from core.dynamodb import utc_now_iso
from core.repositories import container_repo
from core.services import task_address

logger = logging.getLogger(__name__)

# TTL grace period stamped on every connect + ping refresh: how long a row
# survives past its last liveness stamp before DynamoDB's TTL sweep deletes
# it if $disconnect is never called (crashed tab, dropped connection, etc).
_TTL_WINDOW_SECONDS = 7200  # 2 hours


class ConnectionServiceError(Exception):
    """Base exception for connection service errors."""

    pass


class DesktopTargetError(ConnectionServiceError):
    """Base typed failure for desktop execution target resolution."""


class DesktopTargetNotReady(DesktopTargetError):
    """The selected device has no fresh, fully-linked ready connection."""

    code = "desktop_not_ready"


class DesktopTargetStale(DesktopTargetError):
    """The caller selected a generation that is no longer authoritative."""

    code = "desktop_generation_stale"


class DesktopTargetForbidden(DesktopTargetError):
    """The member or command is not in the device's current pairing grant."""

    code = "desktop_forbidden"


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
                    ("task_address", "taskAddress"),
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

    @staticmethod
    def _ttl_is_live(ttl_raw: Optional[str], now: int) -> bool:
        """Shared freshness rule for a presence row's raw ``ttl`` numeric
        attribute value: missing counts as live (over-preserve, defensive
        against an absent attribute or a just-deleted row); expired does not.
        Used by `has_live_operator_connection`.
        """
        return ttl_raw is None or int(ttl_raw) > now

    async def has_live_operator_connection(self, owner_id: str) -> bool:
        """Is a Paperclip run executing against this owner's container right now?

        The gateway relay writes an ``operator``-typed presence row
        (`put_relay_presence`) for the life of the socket the Paperclip
        adapter opens per run, refreshed every ~60s with a 120s ttl
        (`refresh_relay_presence`) and deleted on close — so a still-fresh
        ``operator`` row is a direct read of "is Paperclip using the
        container right now", with no run-state tracking of our own to keep
        in sync.

        Scale-to-zero needs it because Paperclip's operator socket is separate
        from backend-to-OpenClaw room run tracking. Stopping under it kills a
        legitimate turn, violating the stop path's over-preserve rule.

        Freshness-checked (``ttl > now``), unlike the old ``$connect``/
        ``$disconnect``-paired premise: a crashed or OOM'd backend instance
        leaves its presence row behind for up to DynamoDB's TTL sweep lag (up
        to 48h), and a stale row must never veto trial-idle stop for that
        long. ``ttl`` is not on the ``by-owner-id`` GSI's projection, so each
        candidate operator row is fetched individually; a row with no ``ttl``
        at all is treated as live (over-preserve). Same bias if the row is
        deleted between the GSI read and the follow-up ``get_item`` (a
        just-closed connection): the vanished item comes back with no ``ttl``
        attribute, which this method cannot distinguish from "never had one",
        so it counts as live rather than false-negative a real connection.
        """
        now = int(time.time())
        rows = await self.query_by_owner(owner_id)
        for connection_id, _user_id, connection_type in rows:
            if connection_type != "operator":
                continue
            response = await asyncio.to_thread(
                self._client.get_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": connection_id}},
                ProjectionExpression="#ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ConsistentRead=True,
            )
            ttl_raw = response.get("Item", {}).get("ttl", {}).get("N")
            if self._ttl_is_live(ttl_raw, now):
                return True
        return False

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

    RELAY_TTL_SECONDS = 120

    @staticmethod
    def desktop_ready_id(owner_id: str, device_id: str) -> str:
        return f"desktop-ready#{owner_id}#{device_id}"

    async def publish_desktop_ready(self, *, identity, generation: str, commands: set[str]) -> None:
        """CAS the one authoritative ready generation after hello succeeds."""
        key = {"connectionId": {"S": self.desktop_ready_id(identity.owner_id, identity.device_id or "")}}
        current = await asyncio.to_thread(
            self._client.get_item,
            TableName=self.table_name,
            Key=key,
            ProjectionExpression="#generation",
            ExpressionAttributeNames={"#generation": "generation"},
            ConsistentRead=True,
        )
        previous = current.get("Item", {}).get("generation", {}).get("S")
        names = None
        values = None
        condition = "attribute_not_exists(connectionId)"
        if previous is not None:
            condition = "#generation = :previous"
            names = {"#generation": "generation"}
            values = {":previous": {"S": previous}}
        item = {
            **key,
            "userId": {"S": identity.user_id},
            "orgId": {"S": identity.org_id},
            "ownerId": {"S": identity.owner_id},
            "connectionType": {"S": "desktop-ready"},
            "clientId": {"S": identity.client_id},
            "deviceId": {"S": identity.device_id or ""},
            "generation": {"S": generation},
            "commands": {"L": [{"S": command} for command in sorted(commands)]},
            "taskAddress": {"S": task_address.own_address()},
            "ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)},
        }
        kwargs = {
            "TableName": self.table_name,
            "Item": item,
            "ConditionExpression": condition,
        }
        if names is not None:
            kwargs["ExpressionAttributeNames"] = names
        if values is not None:
            kwargs["ExpressionAttributeValues"] = values
        await asyncio.to_thread(self._client.put_item, **kwargs)

    async def desktop_task_address(self, owner_id: str, device_id: str) -> str | None:
        """The task holding this Mac's socket (routing only, no authority); None when not ready."""
        response = await asyncio.to_thread(
            self._client.get_item,
            TableName=self.table_name,
            Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
            ProjectionExpression="taskAddress, #ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ConsistentRead=True,  # a Mac that just reconnected elsewhere routes to its new task
        )
        item = response.get("Item") or {}
        if int(item.get("ttl", {}).get("N", "0")) <= int(time.time()):
            return None
        return item.get("taskAddress", {}).get("S") or None

    async def refresh_desktop_ready(self, owner_id: str, device_id: str, generation: str) -> bool:
        """Refresh only this socket's generation; a superseded socket returns false."""
        try:
            await asyncio.to_thread(
                self._client.update_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
                UpdateExpression="SET #ttl = :ttl, taskAddress = :address",
                ConditionExpression="#generation = :generation",
                ExpressionAttributeNames={"#ttl": "ttl", "#generation": "generation"},
                ExpressionAttributeValues={
                    ":ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)},
                    ":address": {"S": task_address.own_address()},
                    ":generation": {"S": generation},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def delete_desktop_ready(self, owner_id: str, device_id: str, generation: str) -> None:
        """Delete only this socket's generation; an old disconnect cannot erase a new one."""
        try:
            await asyncio.to_thread(
                self._client.delete_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
                ConditionExpression="#generation = :generation",
                ExpressionAttributeNames={"#generation": "generation"},
                ExpressionAttributeValues={":generation": {"S": generation}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    async def _resolve_desktop_target(
        self,
        owner_id: str,
        member_id: str,
        device_id: str,
        generation: str,
        command: str | None,
        *,
        allow_revoking: bool = False,
        allow_ungranted: bool = False,
    ) -> dict:
        response = await asyncio.to_thread(
            self._client.get_item,
            TableName=self.table_name,
            Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or int(item.get("ttl", {}).get("N", "0")) <= int(time.time()):
            raise DesktopTargetNotReady(device_id)
        if item.get("generation", {}).get("S") != generation:
            raise DesktopTargetStale(device_id)
        if (
            item.get("ownerId", {}).get("S") != owner_id
            or item.get("userId", {}).get("S") != member_id
            or item.get("deviceId", {}).get("S") != device_id
        ):
            raise DesktopTargetForbidden(device_id)
        if not item.get("taskAddress", {}).get("S"):
            raise DesktopTargetNotReady(device_id)

        from core.services.desktop_pair import paired_physical_mac

        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        linked = paired_physical_mac(row, member_id, device_id)
        if linked is None:
            raise DesktopTargetNotReady(device_id)
        paired, _peer = linked
        # An unpair in progress: nothing runs on either device of the pair any more, except
        # the backend's own sign-in clear that precedes the removal.
        if not allow_revoking and (paired.get("revoking") or _peer.get("revoking")):
            raise DesktopTargetForbidden(device_id)
        client_id = item.get("clientId", {}).get("S")
        if paired.get("clientId") != client_id:
            raise DesktopTargetNotReady(device_id)
        ready_commands = {value["S"] for value in item.get("commands", {}).get("L", []) if "S" in value}
        from core.services.desktop_pair import granted_commands

        effective = ready_commands & granted_commands(row.get("paired_devices") or {}, paired)
        # None is an internal reciprocal-pair identity read, never a command grant.
        # allow_ungranted: the backend's own revocation clear, never a caller's command.
        if command is not None and command not in effective and not allow_ungranted:
            raise DesktopTargetForbidden(command)
        latest = await asyncio.to_thread(
            self._client.get_item,
            TableName=self.table_name,
            Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
            ProjectionExpression="#generation, #ttl, taskAddress",
            ExpressionAttributeNames={"#generation": "generation", "#ttl": "ttl"},
            ConsistentRead=True,
        )
        latest_item = latest.get("Item")
        if (
            not latest_item
            or int(latest_item.get("ttl", {}).get("N", "0")) <= int(time.time())
            or not latest_item.get("taskAddress", {}).get("S")
        ):
            raise DesktopTargetNotReady(device_id)
        if latest_item.get("generation", {}).get("S") != generation:
            raise DesktopTargetStale(device_id)
        return {
            "taskAddress": latest_item.get("taskAddress", {}).get("S", ""),
            "ownerId": owner_id,
            "memberId": member_id,
            "deviceId": device_id,
            "clientId": client_id,
            "pairingGroupId": paired["pairingGroupId"],
            "generation": generation,
            "commands": sorted(effective),
        }

    async def put_relay_presence(self, *, connection_id: str, identity) -> None:
        """Write the gateway relay's presence row for a node/operator socket.

        Written by the instance that accepted the upgrade, refreshed by that
        same instance every ~60s (`refresh_relay_presence`) and deleted on
        close — the row is how another instance (or a crashed one's TTL
        sweep) learns the connection existed.
        """
        now = int(time.time())
        item = {
            "connectionId": {"S": connection_id},
            "userId": {"S": identity.user_id},
            "orgId": {"S": identity.org_id},
            "ownerId": {"S": identity.owner_id},
            "connectionType": {"S": identity.kind},
            "authKind": {"S": identity.auth_kind},
            "clientId": {"S": identity.client_id},
            "connectedAt": {"S": utc_now_iso()},
            "ttl": {"N": str(now + self.RELAY_TTL_SECONDS)},
            "taskAddress": {"S": task_address.own_address()},
        }
        if identity.device_id:
            item["deviceId"] = {"S": identity.device_id}
        await asyncio.to_thread(self._client.put_item, TableName=self.table_name, Item=item)

    async def refresh_relay_presence(self, connection_id: str) -> bool:
        """Bump the presence row's ttl. False when the row is gone (revoked
        or expired): the caller must close the socket.

        Re-asserts `taskAddress` on every tick (not just at connect) so a row
        never goes addressless -- the 60s refresh loop keeps running for the
        life of the node/operator socket (M2-R1)."""
        try:
            await asyncio.to_thread(
                self._client.update_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": connection_id}},
                UpdateExpression="SET #ttl = :ttl, taskAddress = :addr",
                ConditionExpression="attribute_exists(connectionId)",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)},
                    ":addr": {"S": task_address.own_address()},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ---- a logged-in computer's route link (``network_route``) ---------------------------------

    @staticmethod
    def route_id(key: str) -> str:
        return f"netroute#{key}"

    @staticmethod
    def route_owner(user_id: str) -> str:
        return f"netuser#{user_id}"

    async def routes_for_user(self, user_id: str) -> list[dict]:
        """This person's live computer links (``live_routes`` rows)."""
        found = await self.query_by_owner(self.route_owner(user_id))
        keys = [cid.removeprefix("netroute#") for cid, _, kind in found if kind == "net-route"]
        return [r for r in await self.live_routes(keys) if r["userId"] == user_id] if keys else []

    async def put_route(self, *, key: str, user_id: str, generation: str, name: str = "", org: str = "") -> None:
        """The newest link of computer ``key`` (its member key) wins: one row per key."""
        now = int(time.time())
        item = {
            "computerName": {"S": name or "Your computer"},
            "connectionId": {"S": self.route_id(key)},
            "userId": {"S": user_id},
            # The by-owner-id GSI lists a person's links (``routes_for_user``); no org is ever this.
            "ownerId": {"S": self.route_owner(user_id)},
            "connectionType": {"S": "net-route"},
            "generation": {"S": generation},
            "connectedAt": {"N": str(now)},
            "taskAddress": {"S": task_address.own_address()},
            "ttl": {"N": str(now + self.RELAY_TTL_SECONDS)},
        }
        if org:  # the workspace its proof signed into: a rules push goes to that workspace's links only
            item["org"] = {"S": org}
        await asyncio.to_thread(self._client.put_item, TableName=self.table_name, Item=item)

    async def refresh_route(self, key: str, generation: str) -> bool:
        """False when a newer link of this computer replaced the row (or it expired)."""
        try:
            await asyncio.to_thread(
                self._client.update_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": self.route_id(key)}},
                UpdateExpression="SET #ttl = :ttl, taskAddress = :addr",
                ConditionExpression="#generation = :generation",
                ExpressionAttributeNames={"#ttl": "ttl", "#generation": "generation"},
                ExpressionAttributeValues={
                    ":ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)},
                    ":addr": {"S": task_address.own_address()},
                    ":generation": {"S": generation},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def delete_route(self, key: str, generation: str) -> None:
        """Only this link's row: a closing old link never erases its successor's."""
        try:
            await asyncio.to_thread(
                self._client.delete_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": self.route_id(key)}},
                ConditionExpression="#generation = :generation",
                ExpressionAttributeNames={"#generation": "generation"},
                ExpressionAttributeValues={":generation": {"S": generation}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    async def live_routes(self, keys: list[str]) -> list[dict]:
        """The live (unexpired) route rows of ``keys``: ``{key, userId, generation, connectedAt,
        taskAddress}``, read consistently (a computer that just reconnected routes to its new task)."""
        now = int(time.time())
        out: list[dict] = []
        for i in range(0, len(keys), 100):
            request = {
                self.table_name: {
                    "Keys": [{"connectionId": {"S": self.route_id(k)}} for k in keys[i : i + 100]],
                    "ConsistentRead": True,
                }
            }
            while request:
                resp = await asyncio.to_thread(self._client.batch_get_item, RequestItems=request)
                for item in resp.get("Responses", {}).get(self.table_name, []):
                    if int(item.get("ttl", {}).get("N", "0")) <= now:
                        continue
                    out.append(
                        {
                            "key": item["connectionId"]["S"].removeprefix("netroute#"),
                            "userId": item.get("userId", {}).get("S", ""),
                            "generation": item.get("generation", {}).get("S", ""),
                            "connectedAt": int(item.get("connectedAt", {}).get("N", "0")),
                            "taskAddress": item.get("taskAddress", {}).get("S", ""),
                            "name": item.get("computerName", {}).get("S", ""),
                            "org": item.get("org", {}).get("S", ""),
                        }
                    )
                request = resp.get("UnprocessedKeys") or None
        return out

    async def delete_relay_rows_for_device(self, owner_id: str, device_id: str) -> int:
        """Force-close a revoked device's live relay and ready presence rows.

        The `by-owner-id` GSI does not project `deviceId` (only `userId` /
        `connectionType`), so candidates come from its `node`-typed rows, each
        fetched in full (`get_connection`, base table) to check `deviceId`
        before deleting. At most a handful of rows per owner. Returns the
        number of rows deleted — the desktop-device revoke route uses this in
        place of the old in-process `force_close_device` socket kill; the
        relay itself notices within its 60s presence refresh and closes 1008.
        """
        ready = await asyncio.to_thread(
            self._client.delete_item,
            TableName=self.table_name,
            Key={"connectionId": {"S": self.desktop_ready_id(owner_id, device_id)}},
            ReturnValues="ALL_OLD",
        )
        deleted = int(bool(ready.get("Attributes")))
        for connection_id, _user_id, kind in await self.query_by_owner(owner_id):
            if kind != "node":
                continue
            full = await asyncio.to_thread(self.get_connection, connection_id)
            if full and full.get("device_id") == device_id:
                await asyncio.to_thread(self.delete_connection, connection_id)
                deleted += 1
        return deleted


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


async def resolve_desktop_target(owner_id: str, member_id: str, device_id: str, generation: str, command: str) -> dict:
    """Resolve one explicitly selected, currently-ready desktop device."""
    return await _get_singleton()._resolve_desktop_target(owner_id, member_id, device_id, generation, command)
