"""
Management API Client - pushes messages to WebSocket clients via API Gateway.

This client uses the API Gateway Management API to send messages to connected
WebSocket clients. It handles the outbound direction of the WebSocket flow:

Architecture:
- INBOUND: Client WebSocket -> API Gateway -> HTTP POST -> VPC Link -> ALB -> EC2
- OUTBOUND: EC2 -> POST to Management API -> API Gateway -> Client WebSocket

The Management API endpoint URL format:
https://{api-id}.execute-api.{region}.amazonaws.com/{stage}

Usage:
    client = ManagementApiClient()

    # Send a message to a WebSocket client
    success = client.send_message(connection_id, {"type": "message", "content": "Hello"})

    # Close a WebSocket connection
    client.close_connection(connection_id)
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# API Gateway occasionally returns a spurious GoneException for a connection
# that is still alive (observed ~daily in prod: a single 410 sandwiched
# between hundreds of successful posts to the same connection id). A Gone
# is therefore only treated
# as fatal once it has persisted this long with no successful send in
# between; callers that prune connection state must gate on
# ``gone_confirmed()`` rather than a single ``send_message() is False``.
_GONE_CONFIRM_SECONDS = 2.0

# Safety bound on the suspect map. Entries are normally removed on the next
# successful send or on confirmation; this only catches connections whose
# very last frame ever was a spurious Gone.
_GONE_SUSPECTS_CAP = 4096


class ManagementApiClientError(Exception):
    """Base exception for management API client errors."""

    pass


class ManagementApiClient:
    """
    Client for API Gateway WebSocket Management API.

    Enables the backend to push messages to connected WebSocket clients
    and manage their connections.
    """

    def __init__(self, endpoint_url: Optional[str] = None, region_name: Optional[str] = None):
        """
        Initialize the management API client.

        Args:
            endpoint_url: Management API endpoint URL. If not provided, uses
                         WS_MANAGEMENT_API_URL environment variable.
                         Format: https://{api-id}.execute-api.{region}.amazonaws.com/{stage}
            region_name: AWS region. If not provided, uses AWS_REGION env var.

        Raises:
            ManagementApiClientError: If no endpoint URL is provided and
                                     WS_MANAGEMENT_API_URL env var is not set.
        """
        self.endpoint_url = endpoint_url or os.environ.get("WS_MANAGEMENT_API_URL")

        if not self.endpoint_url:
            raise ManagementApiClientError(
                "No endpoint URL provided. Set WS_MANAGEMENT_API_URL environment "
                "variable or pass endpoint_url parameter."
            )

        # Explicitly set region to avoid NoRegionError in containerized environments
        region = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=self.endpoint_url,
            region_name=region,
        )
        # connection_id -> monotonic time of the first unresolved
        # GoneException. Written from to_thread workers; individual dict ops
        # are GIL-atomic and a race at worst shifts a prune by one frame, so
        # no lock.
        self._gone_first_seen: Dict[str, float] = {}

    def send_message(self, connection_id: str, payload: Dict[str, Any]) -> bool:
        """
        Send a JSON message to a WebSocket client.

        Posts the payload as JSON to the specified connection. This is how
        the backend pushes messages (like streaming LLM responses) to clients.

        Args:
            connection_id: API Gateway WebSocket connection ID
            payload: Dictionary to send (will be JSON encoded)

        Returns:
            True if message was sent successfully, False if connection is gone.

        Raises:
            ManagementApiClientError: If sending fails for reasons other than
                                     a gone connection.
        """
        # Compact separators: matches _trim_chat_history_for_frame's size
        # estimate and keeps every pushed frame under the 128KiB limit longer.
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        try:
            self._client.post_to_connection(
                ConnectionId=connection_id,
                Data=data,
            )
            logger.debug(
                "Sent message to connection %s: %d bytes",
                connection_id,
                len(data),
            )
            # A success proves the connection is alive — any earlier Gone
            # was a spurious API Gateway blip, so restart its clock.
            self._gone_first_seen.pop(connection_id, None)
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "GoneException":
                # Connection is closed - this is expected when clients disconnect
                if len(self._gone_first_seen) > _GONE_SUSPECTS_CAP:
                    self._gone_first_seen.clear()
                self._gone_first_seen.setdefault(connection_id, time.monotonic())
                logger.warning(
                    "Connection %s is gone, message not delivered",
                    connection_id,
                )
                return False

            # Other errors are unexpected and should be raised. boto3
            # sometimes returns Message="" (empty string, not missing), so
            # `dict.get("Message", default)` doesn't fall back — use
            # `or str(e)`. Also surface the error Code + HTTPStatusCode +
            # payload size so PayloadTooLargeException / LimitExceeded
            # are diagnosable without a redeploy.
            error_message = e.response.get("Error", {}).get("Message") or str(e)
            http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            logger.error(
                "Failed to send message to connection %s: code=%s status=%s payload_bytes=%d msg=%s",
                connection_id,
                error_code or "<unknown>",
                http_status,
                len(data),
                error_message,
            )
            raise ManagementApiClientError(
                f"Failed to send message to connection {connection_id}: "
                f"code={error_code or '<unknown>'} status={http_status} "
                f"payload_bytes={len(data)} msg={error_message}"
            ) from e

    async def send_message_async(self, connection_id: str, payload: Dict[str, Any]) -> bool:
        """``send_message`` off the event loop.

        ``send_message`` is a blocking boto3 HTTPS round trip; calling it
        inline from an async handler stalls every coroutine on the loop for
        the duration of the POST. Use this from async code.

        There is no longer a sibling ordered-streaming path here: agent run
        frames left the API Gateway WebSocket for HTTP + AppSync, and
        ``core.gateway.frame_sender`` went with them. This client now serves
        the remaining non-streaming pushes over the WS API.
        """
        return await asyncio.to_thread(self.send_message, connection_id, payload)

    def gone_confirmed(self, connection_id: str) -> bool:
        """Whether Gone has persisted long enough on *connection_id* to treat
        the connection as genuinely dead.

        Callers that destroy connection state on ``send_message() is False``
        (teams-broker unsubscribe) must gate on this:
        a lone GoneException can be a spurious API Gateway response for a
        live connection, and pruning on it orphans a healthy client — its
        pings 401 forever while its socket stays open. Returns True only
        when the first unresolved Gone is at least ``_GONE_CONFIRM_SECONDS``
        old (no successful send since), and consumes the suspect mark so a
        later isolated Gone starts a fresh window.
        """
        first = self._gone_first_seen.get(connection_id)
        if first is None or (time.monotonic() - first) < _GONE_CONFIRM_SECONDS:
            return False
        self._gone_first_seen.pop(connection_id, None)
        return True

    def close_connection(self, connection_id: str) -> None:
        """
        Close a WebSocket connection.

        Forces the WebSocket connection to close. The client will receive
        a close frame. This can be used for session timeouts, forced logouts,
        or cleanup.

        Args:
            connection_id: API Gateway WebSocket connection ID

        Raises:
            ManagementApiClientError: If closing fails for reasons other than
                                     an already-gone connection.
        """
        try:
            self._client.delete_connection(ConnectionId=connection_id)
            logger.info("Closed connection %s", connection_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "GoneException":
                # Connection is already closed - this is fine
                logger.debug(
                    "Connection %s was already closed",
                    connection_id,
                )
                return

            # Other errors are unexpected and should be raised. Same
            # empty-Message defensive pattern as send_message above.
            error_message = e.response.get("Error", {}).get("Message") or str(e)
            http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            logger.error(
                "Failed to close connection %s: code=%s status=%s msg=%s",
                connection_id,
                error_code or "<unknown>",
                http_status,
                error_message,
            )
            raise ManagementApiClientError(
                f"Failed to close connection {connection_id}: "
                f"code={error_code or '<unknown>'} status={http_status} msg={error_message}"
            ) from e
