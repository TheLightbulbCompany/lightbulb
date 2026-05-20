"""Error-path contract tests for ManagementApiClient.

Both send_message and close_connection special-case `GoneException` (410):
the connection is treated as already-disconnected — send_message returns
False, close_connection silently no-ops. Other ClientErrors are raised as
ManagementApiClientError. This file pins those branches so a regression
in core/services/management_api_client.py would fail CI before it can
break stale-connection cleanup or fanout loops in production.

The rest of the surface (boto wiring, payload encoding) is exercised
indirectly via router-level tests and was deemed not worth a unit-level
mock-the-SDK suite (Codex P2 on PR #716 flagged this file's removal).
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services.management_api_client import (
    ManagementApiClient,
    ManagementApiClientError,
)


@pytest.fixture
def client_with_mock():
    mock_apigw = MagicMock()
    with patch("core.services.management_api_client.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_apigw
        client = ManagementApiClient(endpoint_url="https://abc.execute-api.us-east-1.amazonaws.com/prod")
    return client, mock_apigw


def _client_error(code: str, http_status: int = 500) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} error"},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        },
        "PostToConnection",
    )


def test_send_message_returns_false_on_gone_exception(client_with_mock):
    client, mock_apigw = client_with_mock
    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)

    assert client.send_message("conn_gone", {"hello": "world"}) is False


def test_send_message_raises_on_other_client_errors(client_with_mock):
    client, mock_apigw = client_with_mock
    mock_apigw.post_to_connection.side_effect = _client_error("InternalServerError", 500)

    with pytest.raises(ManagementApiClientError, match="Failed to send message"):
        client.send_message("conn_abc", {"hello": "world"})


def test_close_connection_swallows_gone_exception(client_with_mock):
    client, mock_apigw = client_with_mock
    mock_apigw.delete_connection.side_effect = _client_error("GoneException", 410)

    # Already-closed connections must not raise — the caller has no recovery path.
    assert client.close_connection("conn_already_gone") is None


def test_close_connection_raises_on_other_client_errors(client_with_mock):
    client, mock_apigw = client_with_mock
    mock_apigw.delete_connection.side_effect = _client_error("InternalServerError", 500)

    with pytest.raises(ManagementApiClientError, match="Failed to close connection"):
        client.close_connection("conn_abc")
