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

logger = logging.getLogger(__name__)

# TTL grace period stamped on every connect + ping refresh: how long a row
# survives past its last liveness stamp before DynamoDB's TTL sweep deletes
# it if $disconnect is never called (crashed tab, dropped connection, etc).
_TTL_WINDOW_SECONDS = 7200  # 2 hours

# Gateway-liveness freshness window: a gateway-liveness row counts as
# "connected" only if a live GatewayConnection refreshed its lease
# (``refresh_gateway_liveness``) within this window. Must be comfortably
# larger than the connection's refresh cadence (``_LIVENESS_REFRESH_SECONDS``
# in connection_pool, 60s) so a couple of missed/slow refreshes don't flap a
# healthy connection to "not connected", but small enough that a dead or
# crashed gateway (whose refresh loop stopped) reads as disconnected quickly
# rather than lingering as a false "ready" until the ttl sweep. 3 missed
# refreshes.
_LIVENESS_STALE_SECONDS = 180

# Synthetic connectionId namespace for the gateway-liveness rows (see
# mark_gateway_connected). One row per owner_id, keyed
# "gateway#{owner_id}" so it can never collide with a real API Gateway
# connectionId (those are opaque IDs API Gateway mints, never containing
# "#"). This is readiness store R4's replacement: the per-process
# GatewayConnectionPool.is_user_connected read (core/gateway/connection_pool.py)
# moved here so ANY backend instance -- not just the one currently holding
# the WS to the user's OpenClaw container -- can answer "is the gateway
# connected" durably. See
# docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §2
# (Readiness composition) and core/services/readiness_state.py.
_GATEWAY_LIVENESS_PREFIX = "gateway#"


def _gateway_liveness_key(owner_id: str) -> str:
    return f"{_GATEWAY_LIVENESS_PREFIX}{owner_id}"


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

    async def mark_gateway_connected(self, owner_id: str, *, conn_token: str) -> None:
        """Record a live, handshake-verified OpenClaw gateway connection for
        ``owner_id`` -- the durable (cross-instance) liveness signal that
        replaces the in-process ``GatewayConnectionPool.is_user_connected``
        read (readiness store R4). Called by ``GatewayConnection`` at every
        point its own ``is_alive`` flips True: initial ``connect()``
        success and a successful ``reopen()`` (which calls ``connect()``
        internally).

        ``conn_token`` is the owning ``GatewayConnection``'s per-instance
        identity. It is stamped on the row so ``mark_gateway_disconnected``
        can delete ONLY the marker this connection wrote: when a stale
        connection is replaced (or two backend instances overlap during a
        rolling deploy), the replacement's fresh row carries a DIFFERENT
        token, and the old connection's terminal delete -- conditioned on the
        token -- no-ops instead of removing the live marker. An unconditional
        put here is correct: the newest connect claims ownership of the row.

        Best-effort: a DDB write failure is logged and swallowed -- this
        must never break the gateway connect path itself.
        """
        now = int(time.time())
        item = {
            "connectionId": {"S": _gateway_liveness_key(owner_id)},
            "userId": {"S": owner_id},
            "orgId": {"S": ""},
            "ownerId": {"S": owner_id},
            "connectedAt": {"S": utc_now_iso()},
            "connectionType": {"S": "gateway"},
            "connToken": {"S": conn_token},
            "lastPingAt": {"N": str(now)},
            "ttl": {"N": str(now + _TTL_WINDOW_SECONDS)},
        }

        def _put() -> None:
            self._client.put_item(TableName=self.table_name, Item=item)

        try:
            await asyncio.to_thread(_put)
        except ClientError as e:
            logger.warning(
                "mark_gateway_connected failed for owner %s (best-effort, swallowed): %s",
                owner_id,
                e.response["Error"]["Message"],
            )

    async def mark_gateway_disconnected(self, owner_id: str, *, conn_token: str) -> None:
        """Clear THIS connection's gateway-liveness row for ``owner_id``.

        Called by ``GatewayConnection`` at every point its own ``is_alive``
        flips False: the dead branch of ``_on_reader_loop_exit`` (reader
        loop died with no active runs), the parked-watchdog's long-idle
        escalation to dead, and ``close()``. Deliberately NOT called when a
        connection merely parks (``is_alive`` stays True while parked with
        active runs -- see ``GatewayConnection.is_alive``) so a transient
        ping-timeout mid-turn doesn't flip ``/container/status`` back to
        "starting" underneath a live chat.

        CONDITIONED on ``connToken == conn_token``: a replacement connection
        (or a rolling-deploy overlap) will have already written the row with
        ITS token, and this old connection's delete must NOT remove that live
        marker -- the condition makes it a no-op. A missing row (already
        cleared) also fails the condition and is swallowed.

        Best-effort, same swallow posture as ``mark_gateway_connected``.
        """

        def _delete() -> None:
            self._client.delete_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": _gateway_liveness_key(owner_id)}},
                ConditionExpression="connToken = :t",
                ExpressionAttributeValues={":t": {"S": conn_token}},
            )

        try:
            await asyncio.to_thread(_delete)
        except ClientError as e:
            # ConditionalCheckFailed = a newer connection owns the row (or it's
            # already gone) -- the expected benign case, not worth a warning.
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning(
                    "mark_gateway_disconnected failed for owner %s (best-effort, swallowed): %s",
                    owner_id,
                    e.response["Error"]["Message"],
                )

    async def refresh_gateway_liveness(self, owner_id: str, *, conn_token: str) -> None:
        """Extend THIS connection's gateway-liveness lease (lastPingAt + ttl) on
        the periodic tick, WITHOUT stealing ownership.

        Distinct from ``mark_gateway_connected`` (an unconditional put that
        CLAIMS the row -- the newest connect wins): a refresh is conditioned on
        the row being ABSENT or still owned by ``conn_token``. That gives two
        properties at once:
          * missed-connect recovery -- if the initial put was throttled/lost the
            row is absent, so the upsert recreates it as ours; and
          * replacement safety -- a stale-but-still-alive connection (rolling
            deploy overlap) whose refresh fires AFTER a replacement claimed the
            row (newer token) is a conditional no-op, so it can't overwrite the
            row back to the old token and then delete the live replacement's
            marker.

        Best-effort: ConditionalCheckFailed (a newer connection owns it) is the
        expected benign case; other errors are swallowed (next tick retries).
        """
        now = int(time.time())

        def _refresh() -> None:
            self._client.update_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": _gateway_liveness_key(owner_id)}},
                UpdateExpression=(
                    "SET lastPingAt = :p, #ttl = :t, connToken = :tok, connectionType = :ct, ownerId = :o, userId = :o"
                ),
                ConditionExpression="attribute_not_exists(connToken) OR connToken = :tok",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":p": {"N": str(now)},
                    ":t": {"N": str(now + _TTL_WINDOW_SECONDS)},
                    ":tok": {"S": conn_token},
                    ":ct": {"S": "gateway"},
                    ":o": {"S": owner_id},
                },
            )

        try:
            await asyncio.to_thread(_refresh)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning(
                    "refresh_gateway_liveness failed for owner %s (best-effort, swallowed): %s",
                    owner_id,
                    e.response["Error"]["Message"],
                )

    async def get_live_gateway_conn_token(self, owner_id: str) -> str | None:
        """The ``connToken`` of the owner's gateway-liveness row, or ``None``
        when the row is absent, stale, tokenless, or unreadable.

        Same presence + freshness semantics as ``is_gateway_connected``, but
        surfaces WHICH connection owns the row. The grace-path trial stop
        needs the distinction: a fresh row bearing the token of the
        connection that grace cycle itself just closed is that cycle's own
        leftover (its conditioned delete failed) and must NOT read as a
        replacement — otherwise one failed delete inside the freshness
        window vetoes the stop with nothing left to re-arm it, and the
        container runs indefinitely.

        Read failures PROPAGATE — deliberately unlike
        ``is_gateway_connected``, which fails toward False because its
        consumer (readiness) must fail toward the cold-start UI. This
        getter's sole consumer is the stop veto, where "unreadable" must
        mean "abort this stop attempt" (any doubt = don't stop): mapping a
        failure to None would read as "no replacement" and stop a container
        another worker may have just connected to. The stop path's
        catch-all absorbs the raise and skips the stop.
        """
        now_epoch = int(time.time())

        def _get() -> str | None:
            resp = self._client.get_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": _gateway_liveness_key(owner_id)}},
                ProjectionExpression="lastPingAt, connToken",
                # Strongly consistent: this read gates an IRREVERSIBLE
                # container stop, and a replacement's mark_gateway_connected
                # may have landed milliseconds ago on another worker — an
                # eventually-consistent read could miss it or return the
                # superseded token. One 2x-RCU read per stop attempt is
                # nothing; is_gateway_connected stays eventually consistent
                # because its readiness consumer runs on every poll and
                # fails toward the harmless side.
                ConsistentRead=True,
            )
            item = resp.get("Item")
            if item is None:
                return None
            last_ping_raw = item.get("lastPingAt", {}).get("N")
            if last_ping_raw is None:
                return None
            if (now_epoch - int(last_ping_raw)) >= _LIVENESS_STALE_SECONDS:
                return None
            return item.get("connToken", {}).get("S")

        return await asyncio.to_thread(_get)

    async def is_gateway_connected(self, owner_id: str) -> bool:
        """Whether the gateway-liveness row for ``owner_id`` is present AND
        fresh.

        The sole read side of the R4 kill -- consumed by
        ``core.services.readiness_state.compute_status`` (and its
        ``_safe_phase``-equivalent fallback) instead of the old in-process
        ``GatewayConnectionPool.is_user_connected``.

        Freshness matters because DynamoDB's TTL sweep is best-effort and can
        lag hours: an expired row (crashed/uncleanly-torn-down gateway whose
        refresh loop stopped) can linger and would otherwise read as
        connected. So a row counts as connected only if its ``lastPingAt`` is
        within ``_LIVENESS_STALE_SECONDS`` -- i.e. a live ``GatewayConnection``
        has refreshed the lease recently. Row missing OR stale OR read failure
        all read as "not connected" (fail toward the cold-start UI rather than
        a false ``ready``, mirroring the fail-closed posture of the rest of
        the gate machinery in this codebase).
        """
        now_epoch = int(time.time())

        def _get() -> bool:
            resp = self._client.get_item(
                TableName=self.table_name,
                Key={"connectionId": {"S": _gateway_liveness_key(owner_id)}},
                ProjectionExpression="lastPingAt",
            )
            item = resp.get("Item")
            if item is None:
                return False
            last_ping_raw = item.get("lastPingAt", {}).get("N")
            if last_ping_raw is None:
                # A row with no lastPingAt predates the lease refresh -- treat
                # as fresh-enough-if-present is unsafe, so require the stamp.
                return False
            return (now_epoch - int(last_ping_raw)) < _LIVENESS_STALE_SECONDS

        try:
            return await asyncio.to_thread(_get)
        except ClientError as e:
            logger.warning(
                "is_gateway_connected read failed for owner %s (defaulting False): %s",
                owner_id,
                e.response["Error"]["Message"],
            )
            return False


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


async def is_gateway_connected(owner_id: str) -> bool:
    """Module-level helper: the readiness composer's liveness read.

    ``core.services.readiness_state`` imports this rather than
    instantiating its own ``ConnectionService`` so it shares the one
    process-wide singleton (and its boto3 client) with every other caller.
    """
    return await _get_singleton().is_gateway_connected(owner_id)
