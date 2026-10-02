"""Focused tests for machine-only WebSocket $connect admission.

Only the desktop local-session relay uses this door — Tauri node hosts and
Paperclip operators moved to the raw WS relay (routers/gateway_relay.py,
wss://bv[-env].thelightbulb.company/api/v1/gateway/ws) and never reach $connect.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-conns")
    monkeypatch.setenv("ALB_DNS_NAME", "alb.test.local")
    fake_dynamo = MagicMock()
    with patch("boto3.resource", return_value=fake_dynamo):
        sys.modules.pop("index", None)
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import index  # noqa: F401

        yield
        sys.modules.pop("index", None)


def _make_event(
    *,
    client_id="lightbulb-local-sessions",
    auth_kind="device",
    device_id="device-verified",
    user_id="user_alice",
    org_id="",
):
    authorizer = {
        "userId": user_id,
        "orgId": org_id,
        "authKind": auth_kind,
    }
    if device_id is not None:
        authorizer["deviceId"] = device_id
    return {
        "requestContext": {
            "connectionId": "abc123",
            "connectedAt": 1700000000000,
            "authorizer": authorizer,
        },
        "queryStringParameters": (
            {"token": "redacted", "clientId": client_id} if client_id is not None else {"token": "redacted"}
        ),
    }


def _capture_headers():
    captured = {}

    def fake_urlopen(req, timeout=None):
        del timeout
        captured["headers"] = {key.lower(): value for key, value in req.header_items()}
        return MagicMock()

    return captured, fake_urlopen


def _run(event):
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        result = index.handler(event, None)
    return index, result, captured


def test_local_session_device_has_dedicated_type_and_preserves_verified_identity():
    index, result, captured = _run(_make_event())
    assert result == {"statusCode": 200}
    item = index.table.put_item.call_args.kwargs["Item"]
    assert item["connectionType"] == "local-session"
    assert item["authKind"] == "device"
    assert captured["headers"]["x-verified-device-id"] == item["deviceId"] == "device-verified"
    assert captured["headers"]["x-client-id"] == item["clientId"] == "lightbulb-local-sessions"


@pytest.mark.parametrize(
    ("auth_kind", "client_id", "device_id"),
    [
        ("clerk_user", None, None),
        ("clerk_user", "lightbulb-company-room", None),
        ("clerk_user", "some-browser", None),
        ("clerk_user", "lightbulb-local-sessions", "forged"),
        ("paperclip_service", "lightbulb-local-sessions", None),
        ("device", None, "device-verified"),
        # The old node/operator client ids are refused outright now, even
        # with an otherwise-valid device token.
        ("device", "node-host", "device-verified"),
        ("device", "openclaw-macos", "device-verified"),
        ("device", "paperclip-operator", "device-verified"),
        ("device", "lightbulb-local-sessions", None),
        ("paperclip_service", "paperclip-operator", "unverified-device"),
        ("", "lightbulb-local-sessions", "device-verified"),
        ("unknown", "lightbulb-local-sessions", None),
    ],
)
def test_browser_unknown_missing_and_cross_kind_combinations_fail_before_write(auth_kind, client_id, device_id):
    index, result, captured = _run(
        _make_event(
            auth_kind=auth_kind,
            client_id=client_id,
            device_id=device_id,
        )
    )
    assert result == {"statusCode": 403}
    index.table.put_item.assert_not_called()
    assert captured == {}


def test_owner_id_remains_org_or_personal_owner():
    index, _, _ = _run(_make_event(user_id="u_solo"))
    assert index.table.put_item.call_args.kwargs["Item"]["ownerId"] == "u_solo"
    index, _, _ = _run(_make_event(user_id="u_member", org_id="org_team"))
    assert index.table.put_item.call_args.kwargs["Item"]["ownerId"] == "org_team"


def test_attaches_ws_internal_header_when_secret_configured(monkeypatch):
    import index

    monkeypatch.setenv(
        "WS_INTERNAL_SECRET_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:0:secret:ws",
    )
    fake_sm = MagicMock()
    fake_sm.get_secret_value.return_value = {"SecretString": "s3cr3t-value"}
    captured, fake_urlopen = _capture_headers()
    with (
        patch("index.boto3.client", return_value=fake_sm),
        patch("index.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        assert index.handler(_make_event(), None) == {"statusCode": 200}
    assert captured["headers"]["x-lightbulb-ws-internal"] == "s3cr3t-value"


@pytest.mark.parametrize("status", [403, 503])
def test_local_session_handshake_fails_closed_when_backend_does_not_admit_mac(status):
    import index
    from urllib.error import HTTPError

    with patch("index.urllib.request.urlopen", side_effect=HTTPError("http://backend", status, "denied", {}, None)):
        result = index.handler(_make_event(), None)
    assert result == {"statusCode": status}
    index.table.delete_item.assert_called_once_with(Key={"connectionId": "abc123"})


def test_local_session_handshake_fails_closed_on_non_http_error_too():
    """A generic failure (timeout, connection refused) must also 503 and
    delete the row: the old node/operator fire-and-forget fallback is gone
    now that this door admits only the local-session relay."""
    import index

    with patch("index.urllib.request.urlopen", side_effect=RuntimeError("backend unavailable")):
        result = index.handler(_make_event(), None)
    assert result == {"statusCode": 503}
    index.table.delete_item.assert_called_once_with(Key={"connectionId": "abc123"})
