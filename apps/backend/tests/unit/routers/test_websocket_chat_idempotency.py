"""Verify that clientRunId from the agent_chat payload is forwarded as
idempotencyKey to OpenClaw chat.send, mirroring upstream control-ui's
pattern in ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:502.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.websocket_chat import _process_agent_chat_background


@pytest.mark.asyncio
async def test_client_run_id_is_forwarded_as_idempotency_key():
    """If the agent_chat payload includes clientRunId, the backend MUST
    forward it as idempotencyKey on the chat.send RPC params so OpenClaw
    binds the user-turn to that UUID. Without this, OpenClaw assigns its
    own runId and the frontend cannot match the chat.final event to the
    user's send — leaving clientRunIdRef stuck and the queue undrained."""

    fake_pool = MagicMock()
    fake_pool.send_rpc = AsyncMock(return_value={"ok": True})
    fake_pool.mark_run_active = MagicMock()
    fake_pool.gate_chat = AsyncMock(return_value={"blocked": False})

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "t"}, "10.0.0.1"),
    )

    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.get_gateway_pool", return_value=fake_pool),
        patch("routers.websocket_chat.get_ecs_manager", return_value=fake_ecs),
        patch(
            "routers.websocket_chat.get_management_api_client",
            AsyncMock(return_value=fake_mgmt),
        ),
        # get_user_node is imported lazily INSIDE _process_agent_chat_background
        # (from core.services.node_proxy), so it's not a module-level attribute
        # on routers.websocket_chat — patch the source module instead.
        patch("core.services.node_proxy.get_user_node", return_value=None),
    ):
        await _process_agent_chat_background(
            connection_id="c1",
            user_id="user_abc",
            owner_id="user_abc",
            agent_id="agent-xyz",
            message="hello",
            client_run_id="11111111-2222-3333-4444-555555555555",
        )

    # Find the chat.send RPC call among any other RPC calls (e.g., sessions.patch)
    chat_send_calls = [c for c in fake_pool.send_rpc.await_args_list if c.kwargs.get("method") == "chat.send"]
    assert len(chat_send_calls) == 1, "expected exactly one chat.send call"
    params = chat_send_calls[0].kwargs["params"]
    assert params["idempotencyKey"] == "11111111-2222-3333-4444-555555555555"
    assert params["sessionKey"] == "agent:agent-xyz:user_abc"
    assert params["message"] == "hello"


@pytest.mark.asyncio
async def test_missing_client_run_id_falls_back_to_backend_minted_uuid():
    """Backwards-compat: when the frontend doesn't send clientRunId, the
    backend mints its own UUID so OpenClaw's chat.send always has an
    idempotencyKey (preserves the historical contract — see
    ~/Desktop/openclaw/src/gateway/server-methods/chat.ts:2037-2052)."""

    fake_pool = MagicMock()
    fake_pool.send_rpc = AsyncMock(return_value={"ok": True})
    fake_pool.mark_run_active = MagicMock()
    fake_pool.gate_chat = AsyncMock(return_value={"blocked": False})

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "t"}, "10.0.0.1"),
    )

    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.get_gateway_pool", return_value=fake_pool),
        patch("routers.websocket_chat.get_ecs_manager", return_value=fake_ecs),
        patch(
            "routers.websocket_chat.get_management_api_client",
            AsyncMock(return_value=fake_mgmt),
        ),
        # See note on get_user_node patch target above.
        patch("core.services.node_proxy.get_user_node", return_value=None),
    ):
        await _process_agent_chat_background(
            connection_id="c1",
            user_id="user_abc",
            owner_id="user_abc",
            agent_id="agent-xyz",
            message="hello",
            client_run_id=None,
        )

    chat_send_calls = [c for c in fake_pool.send_rpc.await_args_list if c.kwargs.get("method") == "chat.send"]
    assert len(chat_send_calls) == 1
    params = chat_send_calls[0].kwargs["params"]
    # Backend mints a UUID — assert presence + valid UUID format.
    assert "idempotencyKey" in params
    assert isinstance(params["idempotencyKey"], str)
    # uuid4 hex form with dashes: 8-4-4-4-12
    assert len(params["idempotencyKey"]) == 36
    assert params["idempotencyKey"].count("-") == 4
