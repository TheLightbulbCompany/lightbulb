"""Machine-only API Gateway WebSocket relay contract tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import settings
from routers.websocket_chat import router


@pytest.fixture(autouse=True)
def disable_internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", False)


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(router, prefix="/ws")
    return test_app


@pytest.fixture
def connection_service():
    with patch("routers.websocket_chat.get_connection_service", new_callable=AsyncMock) as getter:
        service = MagicMock()
        getter.return_value = service
        yield service


async def _post(app, path, *, headers=None, json=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, headers=headers, json=json)


@pytest.mark.asyncio
async def test_node_connect_requires_exact_verified_provenance_headers(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "node",
        "auth_kind": "device",
        "device_id": "device-1",
    }
    with patch("routers.websocket_chat._kick_off_node_connect", new_callable=AsyncMock) as connect:
        response = await _post(
            app,
            "/ws/connect",
            headers={
                "x-connection-id": "conn-1",
                "x-user-id": "user-1",
                "x-client-id": "node-host",
                "x-auth-kind": "device",
                "x-verified-device-id": "device-1",
            },
        )

    assert response.status_code == 200
    connect.assert_awaited_once_with(
        connection_id="conn-1",
        owner_id="user-1",
        user_id="user-1",
        client_id="node-host",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-client-id", "browser"),
        ("x-auth-kind", "session"),
        ("x-verified-device-id", "wrong-device"),
    ],
)
async def test_internal_secret_does_not_bypass_provenance_mismatch(
    app,
    connection_service,
    monkeypatch,
    header,
    value,
):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "unit-test-internal-secret")
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "node",
        "auth_kind": "device",
        "device_id": "device-1",
    }
    headers = {
        "x-lightbulb-ws-internal": "unit-test-internal-secret",
        "x-connection-id": "conn-1",
        "x-user-id": "user-1",
        "x-client-id": "node-host",
        "x-auth-kind": "device",
        "x-verified-device-id": "device-1",
    }
    headers[header] = value

    assert (await _post(app, "/ws/connect", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_operator_connect_requires_paperclip_provenance(app, connection_service):
    from core.services.paperclip_adapter_config import PAPERCLIP_OPERATOR_CLIENT_ID

    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "operator",
        "auth_kind": "paperclip_service",
    }
    with patch("routers.websocket_chat._kick_off_operator_connect", new_callable=AsyncMock) as connect:
        response = await _post(
            app,
            "/ws/connect",
            headers={
                "x-connection-id": "conn-1",
                "x-user-id": "user-1",
                "x-org-id": "org-1",
                "x-client-id": PAPERCLIP_OPERATOR_CLIENT_ID,
                "x-auth-kind": "paperclip_service",
            },
        )

    assert response.status_code == 200
    connect.assert_awaited_once_with(connection_id="conn-1", owner_id="org-1", user_id="user-1")


@pytest.mark.asyncio
async def test_browser_connect_is_rejected(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "chat",
        "auth_kind": "session",
    }
    response = await _post(
        app,
        "/ws/connect",
        headers={
            "x-connection-id": "conn-1",
            "x-user-id": "user-1",
            "x-client-id": "webchat",
            "x-auth-kind": "session",
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_verified_node_message_uses_tauri_relay(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "node",
        "auth_kind": "device",
        "device_id": "device-1",
    }
    with (
        patch("routers.websocket_chat.is_node_connection", return_value=True),
        patch("routers.websocket_chat.handle_node_message", new_callable=AsyncMock) as relay,
    ):
        response = await _post(
            app,
            "/ws/message",
            headers={"x-connection-id": "conn-1"},
            json={"type": "event", "event": "node.invoke.result"},
        )

    assert response.status_code == 200
    relay.assert_awaited_once()


@pytest.mark.asyncio
async def test_verified_operator_message_uses_paperclip_relay(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "operator",
        "auth_kind": "paperclip_service",
    }
    with (
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.handle_operator_message", new_callable=AsyncMock) as relay,
    ):
        response = await _post(
            app,
            "/ws/message",
            headers={"x-connection-id": "conn-1"},
            json={"type": "event", "event": "paperclip.operator"},
        )

    assert response.status_code == 200
    relay.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_session_raw_envelope_limit_precedes_ingestion(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=AssertionError("oversized input must not be ingested"))
    machine.management.send_message_async = AsyncMock()
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=machine):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1", "content-type": "application/json"},
                content=" " * 16384 + "{}",
            )
    assert response.status_code == 200
    assert machine.management.send_message_async.call_args.args == (
        "conn-1",
        {"type": "local.session.error", "code": "INVALID_REQUEST"},
    )
