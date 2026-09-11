"""Focused tests for machine-only WebSocket $connect admission."""

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
    client_id="node-host",
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


@pytest.mark.parametrize("client_id", ["node-host", "openclaw-macos"])
def test_paired_device_admits_only_node_clients_with_verified_provenance(client_id):
    index, result, captured = _run(_make_event(client_id=client_id))
    assert result == {"statusCode": 200}
    item = index.table.put_item.call_args.kwargs["Item"]
    assert item["connectionType"] == "node"
    assert item["authKind"] == "device"
    assert item["deviceId"] == "device-verified"
    assert item["clientId"] == client_id
    assert captured["headers"]["x-auth-kind"] == "device"
    assert captured["headers"]["x-verified-device-id"] == "device-verified"
    assert captured["headers"]["x-client-id"] == client_id


def test_signed_paperclip_service_admits_only_operator_client():
    index, result, captured = _run(
        _make_event(
            client_id="paperclip-operator",
            auth_kind="paperclip_service",
            device_id=None,
        )
    )
    assert result == {"statusCode": 200}
    item = index.table.put_item.call_args.kwargs["Item"]
    assert item["connectionType"] == "operator"
    assert item["authKind"] == "paperclip_service"
    assert "deviceId" not in item
    assert captured["headers"]["x-auth-kind"] == "paperclip_service"
    assert captured["headers"]["x-client-id"] == "paperclip-operator"
    assert "x-verified-device-id" not in captured["headers"]


def test_local_session_device_has_dedicated_type_and_preserves_verified_identity():
    index, result, captured = _run(_make_event(client_id="lightbulb-local-sessions"))
    assert result == {"statusCode": 200}
    item = index.table.put_item.call_args.kwargs["Item"]
    assert item["connectionType"] == "local-session"
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
        ("device", "lightbulb-local-sessions", None),
        ("device", "paperclip-operator", "device-verified"),
        ("paperclip_service", "node-host", None),
        ("paperclip_service", "openclaw-macos", None),
        ("paperclip_service", "paperclip-operator", "unverified-device"),
        ("device", None, "device-verified"),
        ("device", "node-host", None),
        ("", "node-host", "device-verified"),
        ("unknown", "paperclip-operator", None),
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


def test_no_admitted_row_can_be_chat_or_native_human():
    admitted = [
        _make_event(client_id="node-host"),
        _make_event(client_id="openclaw-macos"),
        _make_event(
            client_id="paperclip-operator",
            auth_kind="paperclip_service",
            device_id=None,
        ),
    ]
    connection_types = []
    for event in admitted:
        index, result, _ = _run(event)
        assert result == {"statusCode": 200}
        connection_types.append(index.table.put_item.call_args.kwargs["Item"]["connectionType"])
    assert set(connection_types) == {"node", "operator"}
    assert "chat" not in connection_types
    assert "native_human" not in connection_types


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


def test_machine_row_remains_for_disconnect_cleanup_if_backend_notification_fails():
    import index

    with patch(
        "index.urllib.request.urlopen",
        side_effect=RuntimeError("backend unavailable"),
    ):
        assert index.handler(_make_event(), None) == {"statusCode": 200}
    assert index.table.put_item.called
    index.table.delete_item.assert_not_called()


@pytest.mark.parametrize("status", [403, 503])
def test_local_session_handshake_fails_closed_when_backend_does_not_admit_mac(status):
    import index
    from urllib.error import HTTPError

    with patch("index.urllib.request.urlopen", side_effect=HTTPError("http://backend", status, "denied", {}, None)):
        result = index.handler(_make_event(client_id="lightbulb-local-sessions"), None)
    assert result == {"statusCode": status}
    index.table.delete_item.assert_called_once_with(Key={"connectionId": "abc123"})
