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

import json
import logging
import os
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


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
        data = json.dumps(payload).encode("utf-8")

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
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "GoneException":
                # Connection is closed - this is expected when clients disconnect
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
