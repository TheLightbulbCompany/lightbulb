"""Error-translation contract tests for ConnectionService.

The service wraps DynamoDB calls and translates every boto `ClientError`
into a `ConnectionServiceError` so callers (websocket lifecycle code) get a consistent exception type to catch.

`get_connection` is the synchronous per-message lookup and
`delete_connection` is the $disconnect hook. A regression that lets a raw
boto error escape either would crash the websocket handler instead of
surfacing as ConnectionServiceError — Codex P2 on PR #716 flagged the loss
of this coverage when the broader test_connection_service.py was deleted as
mostly-mock-the-mock.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services.connection_service import (
    ConnectionService,
    ConnectionServiceError,
)


@pytest.fixture
def service_with_mock():
    mock_ddb = MagicMock()
    with patch("core.services.connection_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ddb
        service = ConnectionService(table_name="test-connections")
    return service, mock_ddb


def _ddb_error(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "DynamoDB error"}},
        op,
    )


def test_get_connection_translates_ddb_error(service_with_mock):
    service, mock_ddb = service_with_mock
    mock_ddb.get_item.side_effect = _ddb_error("GetItem")

    with pytest.raises(ConnectionServiceError, match="Failed to get connection"):
        service.get_connection("conn_abc")


def test_delete_connection_translates_ddb_error(service_with_mock):
    service, mock_ddb = service_with_mock
    mock_ddb.delete_item.side_effect = _ddb_error("DeleteItem")

    with pytest.raises(ConnectionServiceError, match="Failed to delete connection"):
        service.delete_connection("conn_abc")
