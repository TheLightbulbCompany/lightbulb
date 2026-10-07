"""Local-session-only API Gateway WebSocket relay contract tests.

Node hosts and Paperclip operators moved to routers/gateway_relay.py (see
tests/unit/routers/test_gateway_relay.py); this router now serves only the
desktop local-session relay's connect/disconnect/message HTTP integration.
"""

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


_LOCAL_SESSION_HEADERS = {
    "x-connection-id": "conn-1",
    "x-user-id": "user-1",
    "x-client-id": "lightbulb-local-sessions",
    "x-auth-kind": "device",
    "x-verified-device-id": "device-1",
}


@pytest.mark.asyncio
async def test_local_session_connect_succeeds(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "device-1",
    }
    machine = MagicMock()
    machine.connect = AsyncMock()
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=machine):
        response = await _post(app, "/ws/connect", headers=_LOCAL_SESSION_HEADERS)

    assert response.status_code == 200
    machine.connect.assert_awaited_once_with("conn-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-client-id", "browser"),
        ("x-auth-kind", "session"),
        ("x-verified-device-id", "wrong-device"),
    ],
)
async def test_local_session_connect_requires_exact_verified_provenance_headers(
    app,
    connection_service,
    header,
    value,
):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "device-1",
    }
    headers = dict(_LOCAL_SESSION_HEADERS)
    headers[header] = value

    assert (await _post(app, "/ws/connect", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_internal_secret_does_not_bypass_provenance_mismatch(
    app,
    connection_service,
    monkeypatch,
):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "unit-test-internal-secret")
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "device-1",
    }
    headers = dict(_LOCAL_SESSION_HEADERS)
    headers["x-lightbulb-ws-internal"] = "unit-test-internal-secret"
    headers["x-client-id"] = "browser"

    assert (await _post(app, "/ws/connect", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_node_and_operator_connect_are_rejected(app, connection_service):
    """Node hosts and Paperclip operators moved to the gateway relay — this
    door now admits local-session devices only."""
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": "node",
        "auth_kind": "device",
        "device_id": "device-1",
    }
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
    assert response.status_code == 403


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
@pytest.mark.parametrize("connection_type", ["node", "operator", "chat"])
async def test_message_rejects_non_local_session_connections(app, connection_service, connection_type):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": None,
        "connection_type": connection_type,
        "auth_kind": "device",
    }
    response = await _post(
        app,
        "/ws/message",
        headers={"x-connection-id": "conn-1"},
        json={"type": "req", "id": "1", "method": "chat.send"},
    )
    assert response.status_code == 403


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
    with patch(
        "core.services.local_session_relay.get_local_session_relay",
        return_value=machine,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={
                    "x-connection-id": "conn-1",
                    "content-type": "application/json",
                },
                content=" " * 16384 + "{}",
            )
    assert response.status_code == 200
    assert machine.management.send_message_async.call_args.args == (
        "conn-1",
        {"type": "local.session.error", "code": "INVALID_REQUEST"},
    )


@pytest.mark.asyncio
async def test_local_session_error_frame_names_the_rejected_session(app, connection_service):
    from core.services.messaging import ApiProblem

    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed."))
    machine.management.send_message_async = AsyncMock()
    with patch(
        "core.services.local_session_relay.get_local_session_relay",
        return_value=machine,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1"},
                json={
                    "type": "local.session.events",
                    "sessionId": "abc-123",
                    "batchId": "b",
                    "afterSeq": 5,
                    "events": [],
                },
            )
    assert response.status_code == 200
    assert machine.management.send_message_async.call_args.args == (
        "conn-1",
        {
            "type": "local.session.error",
            "code": "LOCAL_SESSION_CONFLICT",
            "sessionId": "abc-123",
        },
    )


@pytest.mark.asyncio
async def test_local_session_rejection_log_never_carries_unsafe_sid_or_frame_text(app, connection_service, caplog):
    import logging

    from core.services.messaging import ApiProblem

    caplog.set_level(logging.WARNING, logger="routers.websocket_chat")
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=ApiProblem("INVALID_REQUEST", "Invalid local session envelope."))
    machine.management.send_message_async = AsyncMock()
    body = {"type": "x" * 200, "sessionId": "pasted terminal output " * 8, "batchId": 1}
    with patch(
        "core.services.local_session_relay.get_local_session_relay",
        return_value=machine,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1"},
                json=body,
            )
    assert response.status_code == 200
    rejected = next(r for r in caplog.records if r.getMessage() == "local session frame rejected")
    assert rejected.sid is None and rejected.frame is None
    assert "pasted terminal output" not in caplog.text
    assert machine.management.send_message_async.call_args.args == (
        "conn-1",
        {"type": "local.session.error", "code": "INVALID_REQUEST"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "answered"),
    [("local.device.heartbeat", True), ("local.session.register", True), ("local.session.events", False)],
)
async def test_unexpected_relay_failure_answers_the_waiting_request_at_once(
    app, connection_service, caplog, frame, answered
):
    import logging

    caplog.set_level(logging.ERROR, logger="routers.websocket_chat")
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=RuntimeError("dynamodb said no"))
    machine.management.send_message_async = AsyncMock()
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=machine):
        response = await _post(
            app,
            "/ws/message",
            headers={"x-connection-id": "conn-1"},
            json={"type": frame, "sessions": [{"title": "private terminal text"}]},
        )
    assert response.status_code == 200
    if answered:
        # No sessionId: INTERNAL answers the desktop's one in-flight request, never quarantines a session.
        assert machine.management.send_message_async.call_args.args == (
            "conn-1",
            {"type": "local.session.error", "code": "INTERNAL"},
        )
    else:
        machine.management.send_message_async.assert_not_awaited()
    failed = next(r for r in caplog.records if r.getMessage() == "local session frame failed")
    assert failed.frame == frame and failed.cid == "conn-1"
    assert "private terminal text" not in caplog.text


@pytest.mark.asyncio
async def test_a_screen_frame_is_forwarded_and_never_answered(app, connection_service):
    connection_service.get_connection.return_value = {
        "user_id": "user-1",
        "org_id": "org-1",
        "connection_type": "local-session",
        "auth_kind": "device",
        "client_id": "lightbulb-local-sessions",
        "device_id": "a" * 64,
    }
    machine = MagicMock()
    machine.handle_message = AsyncMock(side_effect=AssertionError("screen frames skip the RPC path"))
    machine.management.send_message_async = AsyncMock()
    body = {"type": "local.session.screen", "sessionId": "s", "data": "x"}
    with (
        patch("core.services.local_session_relay.get_local_session_relay", return_value=machine),
        patch("core.services.session_screen.forward", AsyncMock()) as forward,
    ):
        response = await _post(app, "/ws/message", headers={"x-connection-id": "conn-1"}, json=body)
        oversized = await _post(
            app, "/ws/message", headers={"x-connection-id": "conn-1"}, json={**body, "data": "x" * 16384}
        )
    assert response.status_code == oversized.status_code == 200
    forward.assert_awaited_once_with(machine, "conn-1", body)
    machine.management.send_message_async.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["local.device.heartbeat", "local.session.screen"])
async def test_a_slow_frame_says_where_its_time_went(app, connection_service, caplog, monkeypatch, frame):
    """Prod 2026-10-07: a heartbeat whose own work took 355 ms was answered over 10 s later and
    nothing logged said why. One line per slow frame: handler, reply, thread pool, event loop."""
    import logging

    monkeypatch.setattr("routers.websocket_chat._SLOW_FRAME_S", 0)
    caplog.set_level(logging.WARNING, logger="routers.websocket_chat")
    connection_service.get_connection.return_value = {"connection_type": "local-session"}
    machine = MagicMock()
    machine.handle_message = AsyncMock(return_value={"type": "local.device.heartbeat.ack"})
    machine.management.send_message_async = AsyncMock()
    with (
        patch("core.services.local_session_relay.get_local_session_relay", return_value=machine),
        patch("core.services.session_screen.forward", AsyncMock()),
    ):
        body = {"type": frame, "sessions": [{"title": "private terminal text"}]}
        assert (await _post(app, "/ws/message", headers={"x-connection-id": "conn-1"}, json=body)).status_code == 200
    slow = next(r for r in caplog.records if r.getMessage() == "local session frame slow")
    assert slow.frame == frame and slow.cid == "conn-1"
    assert all(isinstance(getattr(slow, key), int) for key in ("ms", "handle_ms", "pool_wait_ms", "loop_lag_ms"))
    assert "private terminal text" not in caplog.text
