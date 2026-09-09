"""
Contract tests for WebSocket HTTP POST routes.

These routes are HTTP endpoints called by API Gateway when WebSocket events
occur ($connect, $disconnect, $default). They use custom headers from the
Lambda authorizer context, not standard auth.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext


@pytest.fixture
async def ws_client():
    """
    Async test client for WebSocket route contract tests.

    Mocks ConnectionService (DynamoDB) and ManagementApiClient so tests
    run without AWS infrastructure.
    """
    from main import app
    from core.auth import get_current_user

    async def mock_get_current_user():
        return AuthContext(user_id="ws_test_user")

    app.dependency_overrides[get_current_user] = mock_get_current_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# --- /ws/connect ---


@pytest.mark.asyncio
async def test_ws_connect_requires_connection_id(ws_client):
    """POST /ws/connect without x-connection-id returns 400."""
    response = await ws_client.post(
        "/api/v1/ws/connect",
        headers={"x-user-id": "user_123"},
    )
    assert response.status_code == 400
    assert "connection-id" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_ws_connect_requires_user_id(ws_client):
    """POST /ws/connect without x-user-id returns 401."""
    response = await ws_client.post(
        "/api/v1/ws/connect",
        headers={"x-connection-id": "conn_abc"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_ws_connect_success(ws_client):
    """POST /ws/connect with valid headers returns 200."""
    with patch("routers.websocket_chat.get_connection_service") as mock_cs:
        mock_cs.return_value.store_connection = MagicMock()
        response = await ws_client.post(
            "/api/v1/ws/connect",
            headers={
                "x-connection-id": "conn_abc",
                "x-user-id": "user_123",
            },
        )
    assert response.status_code == 200


# --- /ws/disconnect ---


@pytest.mark.asyncio
async def test_ws_disconnect_without_connection_id(ws_client):
    """POST /ws/disconnect without connection ID returns 200 (no-op)."""
    response = await ws_client.post("/api/v1/ws/disconnect")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ws_disconnect_success(ws_client):
    """POST /ws/disconnect with connection ID returns 200."""
    service = MagicMock()
    service.get_connection.return_value = None
    with patch("routers.websocket_chat.get_connection_service", return_value=service):
        response = await ws_client.post(
            "/api/v1/ws/disconnect",
            headers={"x-connection-id": "conn_abc"},
        )
    assert response.status_code == 200
    service.get_connection.assert_called_once_with("conn_abc")
    service.delete_connection.assert_called_once_with("conn_abc")


# --- /ws/message ---


@pytest.mark.asyncio
async def test_ws_message_requires_connection_id(ws_client):
    """POST /ws/message without x-connection-id returns 400."""
    response = await ws_client.post(
        "/api/v1/ws/message",
        json={"type": "ping"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_ws_message_unknown_connection_returns_401(ws_client):
    """POST /ws/message with unknown connection returns 401."""
    mock_service = MagicMock()
    mock_service.get_connection.return_value = None
    with patch("routers.websocket_chat.get_connection_service", new_callable=AsyncMock, return_value=mock_service):
        response = await ws_client.post(
            "/api/v1/ws/message",
            json={"type": "ping"},
            headers={"x-connection-id": "conn_unknown"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_ws_message_ping_returns_200(ws_client):
    """POST /ws/message with ping type returns 200."""
    mock_service = MagicMock()
    mock_service.get_connection.return_value = {
        "user_id": "user_123",
        "org_id": None,
    }
    mock_mgmt_instance = MagicMock()
    mock_mgmt_instance.post_to_connection = AsyncMock()
    mock_mgmt_instance.send_message_async = AsyncMock(side_effect=mock_mgmt_instance.send_message)
    with (
        patch("routers.websocket_chat.get_connection_service", new_callable=AsyncMock, return_value=mock_service),
        patch(
            "routers.websocket_chat.get_management_api_client", new_callable=AsyncMock, return_value=mock_mgmt_instance
        ),
    ):
        response = await ws_client.post(
            "/api/v1/ws/message",
            json={"type": "ping"},
            headers={"x-connection-id": "conn_abc"},
        )
    assert response.status_code == 200


# --- OpenAPI spec verification ---


@pytest.mark.asyncio
async def test_ws_endpoints_documented_in_openapi(ws_client):
    """All WebSocket routes should appear in the OpenAPI spec."""
    response = await ws_client.get("/api/v1/openapi.json")
    spec = response.json()
    paths = spec["paths"]
    assert "/api/v1/ws/connect" in paths
    assert "/api/v1/ws/disconnect" in paths
    assert "/api/v1/ws/message" in paths


@pytest.mark.asyncio
async def test_ws_endpoints_tagged_as_websocket(ws_client):
    """WebSocket routes should be tagged 'websocket'."""
    response = await ws_client.get("/api/v1/openapi.json")
    spec = response.json()
    for path in ["/api/v1/ws/connect", "/api/v1/ws/disconnect", "/api/v1/ws/message"]:
        post = spec["paths"][path]["post"]
        assert "websocket" in post.get("tags", []), f"{path} missing 'websocket' tag"


@pytest.mark.asyncio
async def test_ws_connect_documents_error_responses(ws_client):
    """ws_connect should document 400 and 401 error responses."""
    response = await ws_client.get("/api/v1/openapi.json")
    spec = response.json()
    connect = spec["paths"]["/api/v1/ws/connect"]["post"]
    assert "400" in connect["responses"]
    assert "401" in connect["responses"]


@pytest.mark.asyncio
async def test_ws_message_documents_error_responses(ws_client):
    """ws_message should document 400 and 401 error responses."""
    response = await ws_client.get("/api/v1/openapi.json")
    spec = response.json()
    message = spec["paths"]["/api/v1/ws/message"]["post"]
    assert "400" in message["responses"]
    assert "401" in message["responses"]
