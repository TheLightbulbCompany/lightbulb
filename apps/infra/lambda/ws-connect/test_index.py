"""Tests for $connect Lambda forwarding semantics."""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    """Stub env + boto3 before index module imports."""
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-conns")
    monkeypatch.setenv("ALB_DNS_NAME", "alb.test.local")
    # Force a fresh import per test so the module-level boto3 client picks up
    # the patched mock.
    fake_dynamo = MagicMock()
    with patch("boto3.resource", return_value=fake_dynamo):
        sys.modules.pop("index", None)
        # Ensure the lambda dir is importable
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import index  # noqa: F401
        yield
        sys.modules.pop("index", None)


def _make_event(*, qs=None, headers=None, connection_id="abc123", user_id="user_alice", org_id=None):
    return {
        "requestContext": {
            "connectionId": connection_id,
            "connectedAt": 1700000000000,
            "authorizer": {"userId": user_id, "orgId": org_id or ""} if user_id else {},
        },
        "queryStringParameters": qs or {},
        "headers": headers or {},
    }


def _capture_headers():
    """Return (captured_dict, fake_urlopen) — capture headers sent to backend."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        # urllib.request.Request stores headers in a dict with title-cased keys
        captured["headers"] = dict(req.header_items())
        # Normalize back to lower-case for assertions
        captured["headers_lc"] = {k.lower(): v for k, v in captured["headers"].items()}
        return MagicMock()

    return captured, fake_urlopen


def test_handler_forwards_client_id_header_when_query_param_set():
    """When ?clientId=node-host is in URL, lambda must forward x-client-id header."""
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        result = index.handler(
            _make_event(qs={"token": "j", "clientId": "node-host"}), None
        )
    assert result == {"statusCode": 200}
    assert captured["headers_lc"].get("x-client-id") == "node-host"


def test_handler_forwards_openclaw_macos_client_id():
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(
            _make_event(qs={"token": "j", "clientId": "openclaw-macos"}), None
        )
    assert captured["headers_lc"].get("x-client-id") == "openclaw-macos"


def test_handler_omits_x_client_id_when_no_query_param():
    """No clientId in URL → no x-client-id header sent."""
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(_make_event(qs={"token": "j"}), None)
    assert "x-client-id" not in captured["headers_lc"]


def test_handler_omits_x_client_id_when_no_query_string_at_all():
    """queryStringParameters=None (API GW shape when no params) → no header."""
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        event = _make_event()
        event["queryStringParameters"] = None
        index.handler(event, None)
    assert "x-client-id" not in captured["headers_lc"]
