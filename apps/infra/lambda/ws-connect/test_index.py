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


def _put_item_for(qs=None, **event_kwargs):
    """Run the handler and return the Item dict written to DynamoDB put_item."""
    import index

    _, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(_make_event(qs=qs, **event_kwargs), None)
    # index.table is fake_dynamo.Table(...) — a MagicMock; inspect its put_item call.
    item = index.table.put_item.call_args.kwargs["Item"]
    return item


def test_connect_writes_connection_type_node():
    assert _put_item_for(qs={"clientId": "node-host"})["connectionType"] == "node"


def test_native_room_uses_verified_context_not_external_headers():
    import index

    event = _make_event(qs={"clientId": "lightbulb-company-room", "verifiedIssuer": "forged"},
        org_id="org_team", headers={"x-openclaw-scopes": "operator.admin", "x-user-id": "forged", "x-verified-issuer": "forged"})
    event["requestContext"]["authorizer"].update(authKind="clerk_user", verifiedIssuer="https://verified.test", verifiedSubject="user_alice")
    event["requestContext"]["identity"] = {"sourceIp": "198.51.100.10"}
    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        assert index.handler(event, None)["statusCode"] == 200
    stored = index.table.put_item.call_args.kwargs["Item"]
    assert stored["connectionType"] == "native_human"
    assert stored["verifiedIssuer"] == "https://verified.test"
    assert stored["verifiedSubject"] == "user_alice"
    assert stored["sourceIp"] == "198.51.100.10"
    assert "x-openclaw-scopes" not in captured["headers_lc"]
    assert captured["headers_lc"]["x-user-id"] == "user_alice"


@pytest.mark.parametrize("auth_kind", ["", "device", "service"])
def test_native_room_rejects_unverified_and_nonhuman_auth(auth_kind):
    import index

    event = _make_event(qs={"clientId": "lightbulb-company-room"}, org_id="org_team")
    event["requestContext"]["authorizer"].update(authKind=auth_kind, verifiedIssuer="https://verified.test", verifiedSubject="user_alice")
    assert index.handler(event, None)["statusCode"] == 403
    index.table.put_item.assert_not_called()


def test_native_room_backend_refusal_does_not_leave_connection_admitted():
    import index

    event = _make_event(qs={"clientId": "lightbulb-company-room"}, org_id="org_team")
    event["requestContext"]["authorizer"].update(authKind="clerk_user", verifiedIssuer="https://verified.test", verifiedSubject="user_alice")
    with patch("index.urllib.request.urlopen", side_effect=RuntimeError("qualification unavailable")):
        assert index.handler(event, None)["statusCode"] == 403
    index.table.delete_item.assert_called_once_with(Key={"connectionId": "abc123"})
    assert _put_item_for(qs={"clientId": "openclaw-macos"})["connectionType"] == "node"


def test_connect_writes_connection_type_operator():
    assert _put_item_for(qs={"clientId": "paperclip-operator"})["connectionType"] == "operator"


def test_connect_defaults_connection_type_chat():
    # Browser chat (no clientId) and any unknown clientId both default to "chat".
    assert _put_item_for(qs={})["connectionType"] == "chat"
    assert _put_item_for(qs={"clientId": "some-browser"})["connectionType"] == "chat"


def test_connect_writes_owner_id_personal_and_org():
    # personal: ownerId == userId
    assert _put_item_for(user_id="u_solo", org_id="")["ownerId"] == "u_solo"
    # org: ownerId == orgId
    assert _put_item_for(user_id="u_member", org_id="org_z")["ownerId"] == "org_z"


def test_attaches_ws_internal_header_when_secret_configured(monkeypatch):
    """With WS_INTERNAL_SECRET_SECRET_ARN set, the Lambda fetches the secret and
    attaches it as x-lightbulb-ws-internal so the backend can authenticate the caller."""
    import index

    monkeypatch.setenv("WS_INTERNAL_SECRET_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:0:secret:ws")
    fake_sm = MagicMock()
    fake_sm.get_secret_value.return_value = {"SecretString": "s3cr3t-value"}
    captured, fake_urlopen = _capture_headers()
    with patch("index.boto3.client", return_value=fake_sm), patch(
        "index.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        index.handler(_make_event(qs={"token": "j"}), None)
    assert captured["headers_lc"].get("x-lightbulb-ws-internal") == "s3cr3t-value"


def test_omits_ws_internal_header_when_unconfigured():
    """No WS_INTERNAL_SECRET_SECRET_ARN → no header (backend then runs unenforced)."""
    import index

    captured, fake_urlopen = _capture_headers()
    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(_make_event(qs={"token": "j"}), None)
    assert "x-lightbulb-ws-internal" not in captured["headers_lc"]
