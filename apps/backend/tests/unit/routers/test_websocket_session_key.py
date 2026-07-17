"""Write-path session-key threading: session_id -> server-built 4-part key."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from routers.websocket_chat import (  # noqa: E402
    router,
    _process_agent_abort_background,
    _process_agent_chat_background,
    _process_agent_steer_background,
)

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture
def mock_ecs():
    with patch("routers.websocket_chat.get_ecs_manager") as g:
        m = AsyncMock()
        m.resolve_running_container = AsyncMock(
            return_value=({"gateway_token": "tok", "owner_id": "user-1"}, "10.0.1.5")
        )
        g.return_value = m
        yield m


@pytest.fixture
def mock_pool():
    with patch("routers.websocket_chat.get_gateway_pool") as g:
        p = AsyncMock()
        p.send_rpc = AsyncMock(return_value={"runId": "r"})
        p.gate_chat = AsyncMock(return_value={"blocked": False})
        p.mark_run_active = MagicMock()
        p.clear_active_run = MagicMock()
        p.mark_side_run = MagicMock()
        g.return_value = p
        yield p


@pytest.fixture
def mock_mgmt():
    with patch("routers.websocket_chat.get_management_api_client") as g:
        c = MagicMock()
        c.send_message = MagicMock(return_value=True)
        c.send_message_async = AsyncMock(side_effect=c.send_message)
        g.return_value = c
        yield c


@pytest.mark.asyncio
async def test_send_builds_4part_key_and_gate_still_fires(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_chat_background(
        connection_id="c",
        user_id="user-1",
        owner_id="user-1",
        agent_id="luna",
        message="hi",
        session_id=_UUID,
    )
    params = mock_pool.send_rpc.call_args[1]["params"]
    assert params["sessionKey"] == f"agent:luna:user-1:{_UUID}"
    # Send MUST stay on the gated path — gate_chat is the subscription/credit gate.
    mock_pool.gate_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_legacy_key_when_no_session_id(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_chat_background(
        connection_id="c",
        user_id="user-1",
        owner_id="user-1",
        agent_id="luna",
        message="hi",
    )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == "agent:luna:user-1"


@pytest.mark.asyncio
async def test_abort_builds_4part_key(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_abort_background(
        connection_id="c",
        user_id="user-1",
        owner_id="user-1",
        agent_id="luna",
        run_id="run-9",
        session_id=_UUID,
    )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == f"agent:luna:user-1:{_UUID}"


@pytest.mark.asyncio
async def test_steer_builds_4part_key(mock_ecs, mock_pool, mock_mgmt):
    # The steer path consults the free-tier daily cap before forwarding; pin it
    # under-cap so the read-path stays isolated from billing_repo in this
    # session-key test.
    with patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)):
        await _process_agent_steer_background(
            connection_id="c",
            user_id="user-1",
            owner_id="user-1",
            agent_id="luna",
            text="stop",
            session_id=_UUID,
        )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == f"agent:luna:user-1:{_UUID}"


@pytest.mark.asyncio
async def test_handler_rejects_invalid_session_id_before_queue(mock_mgmt):
    """A malformed session_id is rejected at the HTTP handler; no bg task queued."""
    app = FastAPI()
    app.include_router(router, prefix="/ws")
    with (
        patch("routers.websocket_chat.get_connection_service") as conn,
        patch("routers.websocket_chat._process_agent_chat_background") as bg,
    ):
        # get_connection_service is async -> patch makes it an AsyncMock, whose
        # child get_connection would return a coroutine. Pin the awaited service
        # to a plain (sync) MagicMock so get_connection yields the dict directly,
        # matching the fixture pattern in test_websocket_agent_chat.py.
        conn.return_value = MagicMock()
        conn.return_value.get_connection.return_value = {"user_id": "user-1", "org_id": None}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1"},
                json={"type": "agent_chat", "agent_id": "luna", "message": "hi", "session_id": "../evil"},
            )
    assert resp.status_code == 200
    bg.assert_not_called()
    assert mock_mgmt.send_message.call_args[0][1]["type"] == "error"
