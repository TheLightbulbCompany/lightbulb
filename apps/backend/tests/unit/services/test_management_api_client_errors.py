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


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _install_clock(monkeypatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr("core.services.management_api_client.time", clock)
    return clock


def test_single_gone_is_suspect_not_confirmed(client_with_mock, monkeypatch):
    """API Gateway emits spurious 410s for live connections (~daily in prod:
    one 410 sandwiched between successful posts). One Gone must only mark the
    connection suspect — pruning on it zombifies a healthy session."""
    client, mock_apigw = client_with_mock
    _install_clock(monkeypatch)
    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)

    assert client.send_message("conn_a", {"x": 1}) is False
    assert client.gone_confirmed("conn_a") is False


def test_gone_confirmed_once_it_persists_past_window(client_with_mock, monkeypatch):
    client, mock_apigw = client_with_mock
    clock = _install_clock(monkeypatch)
    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)

    assert client.send_message("conn_a", {"x": 1}) is False
    clock.now += 2.1
    assert client.send_message("conn_a", {"x": 2}) is False
    assert client.gone_confirmed("conn_a") is True
    # Confirmation consumes the suspect mark: a later isolated Gone starts over.
    assert client.gone_confirmed("conn_a") is False


def test_successful_send_clears_gone_suspicion(client_with_mock, monkeypatch):
    """A success after a Gone proves the connection is alive — the earlier 410
    was a blip. The suspect clock must restart from zero on the next Gone."""
    client, mock_apigw = client_with_mock
    clock = _install_clock(monkeypatch)

    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)
    assert client.send_message("conn_a", {"x": 1}) is False

    mock_apigw.post_to_connection.side_effect = None
    assert client.send_message("conn_a", {"x": 2}) is True

    clock.now += 10.0
    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)
    assert client.send_message("conn_a", {"x": 3}) is False
    assert client.gone_confirmed("conn_a") is False


def test_gone_confirmation_is_per_connection(client_with_mock, monkeypatch):
    client, mock_apigw = client_with_mock
    clock = _install_clock(monkeypatch)
    mock_apigw.post_to_connection.side_effect = _client_error("GoneException", 410)

    assert client.send_message("conn_a", {"x": 1}) is False
    clock.now += 2.1
    assert client.send_message("conn_b", {"x": 1}) is False
    assert client.gone_confirmed("conn_b") is False
    assert client.gone_confirmed("conn_a") is True


def test_constructor_fails_fast_when_endpoint_url_missing(monkeypatch):
    """No endpoint URL (neither arg nor WS_MANAGEMENT_API_URL env var) must
    raise at construction time — not defer the failure into the first
    websocket fanout where it's harder to diagnose."""
    monkeypatch.delenv("WS_MANAGEMENT_API_URL", raising=False)

    with pytest.raises(ManagementApiClientError, match="No endpoint URL"):
        ManagementApiClient()
