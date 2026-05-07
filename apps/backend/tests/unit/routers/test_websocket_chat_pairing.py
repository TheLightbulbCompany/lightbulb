"""Tests for /chat agents.create → Paperclip-first interception."""

import os

# Some imports in routers.websocket_chat eagerly read env vars.
# Set placeholders BEFORE importing to avoid module-load failures
# in unit-test contexts.
os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://stub-management-api.local")

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_chat_agents_create_calls_paperclip_first_then_pairs(monkeypatch):
    """When /chat sends agents.create over WS, the backend
    1. creates a Paperclip agent (under the owner's company), gets pc_uuid
    2. pairs it with the OWNER's container (org's container if member)
    3. agents.create RPC is consumed; not forwarded to gateway
    4. interception itself sends the WS response (cleaner contract:
       interception fully owns the dispatcher's slot).

    user_id = the authenticated member; owner_id = container key.
    They differ when the user is an org member."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    pc_create_mock = AsyncMock(
        return_value={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"}
    )
    pair_mock = AsyncMock()

    monkeypatch.setattr(
        "routers.websocket_chat._paperclip_create_for_chat",
        pc_create_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.pair_with_openclaw",
        pair_mock,
    )

    # Mock the management API singleton — interception sends the
    # response itself, so the dispatcher doesn't need to.
    mgmt_mock = MagicMock()
    mgmt_mock.send_message = MagicMock(return_value=True)
    monkeypatch.setattr(
        "routers.websocket_chat.get_management_api_client",
        AsyncMock(return_value=mgmt_mock),
    )

    from routers.websocket_chat import _intercept_agents_create

    consumed = await _intercept_agents_create(
        connection_id="conn-abc",
        req_id="req-1",
        user_id="user_member_b",
        owner_id="org_xyz",
        params={"name": "Code Reviewer", "role": "engineer"},
        ip="10.0.0.5",
        token="gw_token",
    )

    pc_create_mock.assert_awaited_once()
    # owner_id passed to paperclip-create (for company lookup) is org_xyz.
    assert pc_create_mock.await_args.kwargs["owner_id"] == "org_xyz"
    # user_id passed for adapterConfig.sessionKey is the member.
    assert pc_create_mock.await_args.kwargs["creator_user_id"] == "user_member_b"

    pair_mock.assert_awaited_once()
    assert pair_mock.await_args.kwargs["owner_id"] == "org_xyz"
    assert pair_mock.await_args.kwargs["paperclip_agent"]["id"] == pc_uuid
    assert pair_mock.await_args.kwargs["ip"] == "10.0.0.5"
    assert pair_mock.await_args.kwargs["token"] == "gw_token"

    # Interception consumed the RPC and emitted the response itself.
    assert consumed is True
    mgmt_mock.send_message.assert_called_once()
    target_conn, frame = mgmt_mock.send_message.call_args.args
    assert target_conn == "conn-abc"
    assert frame["type"] == "res"
    assert frame["id"] == "req-1"
    assert frame["ok"] is True
    # Response surfaces the new agent UUID so the frontend can use it.
    assert frame["payload"]["agentId"] == pc_uuid


@pytest.mark.asyncio
async def test_chat_agents_create_with_no_name_falls_through_to_gateway(monkeypatch):
    """If the RPC has no name, don't try to interpret it — let OpenClaw's
    own validator reject. We don't duplicate validation."""
    pair_mock = AsyncMock()
    pc_create_mock = AsyncMock()
    monkeypatch.setattr(
        "routers.websocket_chat._paperclip_create_for_chat", pc_create_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.pair_with_openclaw", pair_mock,
    )

    mgmt_mock = MagicMock()
    mgmt_mock.send_message = MagicMock(return_value=True)
    monkeypatch.setattr(
        "routers.websocket_chat.get_management_api_client",
        AsyncMock(return_value=mgmt_mock),
    )

    from routers.websocket_chat import _intercept_agents_create

    consumed = await _intercept_agents_create(
        connection_id="conn-abc",
        req_id="req-1",
        user_id="user_a",
        owner_id="user_a",
        params={"role": "engineer"},  # no name
        ip="10.0.0.5",
        token="gw_token",
    )

    # Falls through — dispatcher should forward the call to the gateway,
    # which will reject with INVALID_REQUEST.
    assert consumed is False
    pc_create_mock.assert_not_awaited()
    pair_mock.assert_not_awaited()
    # No response emitted by the interception — dispatcher handles it.
    mgmt_mock.send_message.assert_not_called()
