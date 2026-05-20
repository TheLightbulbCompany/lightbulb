"""Tests for chat-path Paperclip-meta wiring on agents.create + agents.update."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://stub-management-api.local")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from routers.websocket_chat import (  # noqa: E402
    _intercept_agents_create,
    _intercept_agents_update,
)


@pytest.mark.asyncio
async def test_intercept_create_reads_paperclip_meta_from_params():
    """When the chat client sends params={"name": "Ray", "params":
    {"paperclip": {"role": "engineer", "title": "...", "capabilities": "..."}}},
    the interceptor passes a PaperclipMeta to create_paperclip_for_chat."""
    create_call = AsyncMock(return_value={"id": "agent-uuid", "name": "Ray"})
    mock_pair = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.send_rpc = AsyncMock()
    mock_mgmt = MagicMock()
    mock_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=mock_pair),
        patch("routers.websocket_chat.get_gateway_pool", return_value=mock_pool),
        patch(
            "routers.websocket_chat.get_management_api_client",
            new=AsyncMock(return_value=mock_mgmt),
        ),
    ):
        consumed = await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="user_creator",
            owner_id="user_owner",
            params={
                "name": "Ray",
                "params": {
                    "paperclip": {
                        "role": "engineer",
                        "title": "Backend dev",
                        "capabilities": "Writes Python.",
                    }
                },
            },
            ip="10.0.0.1",
            token="tok",
        )

    assert consumed is True
    # create_paperclip_for_chat got a PaperclipMeta with the user's fields
    kwargs = create_call.await_args.kwargs
    assert kwargs["meta"] is not None
    assert kwargs["meta"].role == "engineer"
    assert kwargs["meta"].title == "Backend dev"
    assert kwargs["meta"].capabilities == "Writes Python."


@pytest.mark.asyncio
async def test_intercept_create_with_no_meta_works_as_before():
    """No params.paperclip — meta=None, behavior identical to today."""
    create_call = AsyncMock(return_value={"id": "agent-uuid", "name": "Ray"})

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=AsyncMock()),
        patch("routers.websocket_chat.get_gateway_pool", return_value=MagicMock()),
        patch(
            "routers.websocket_chat.get_management_api_client",
            new=AsyncMock(return_value=MagicMock()),
        ),
    ):
        await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="u",
            owner_id="u",
            params={"name": "Ray"},
            ip="10.0.0.1",
            token="tok",
        )

    kwargs = create_call.await_args.kwargs
    # meta arg either absent (existing signature) or None
    assert kwargs.get("meta") is None


@pytest.mark.asyncio
async def test_intercept_update_calls_paperclip_update_when_meta_present():
    """agents.update with params.paperclip in the RPC body triggers a
    parallel PATCH to Paperclip. Returns False so the dispatcher still
    forwards to OpenClaw."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "agent-uuid"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="sess=c"),
        ),
    ):
        consumed = await _intercept_agents_update(
            owner_id="user_owner",
            params={
                "agentId": "agent-uuid",
                "name": "Renamed",
                "params": {"paperclip": {"role": "engineer", "title": "Senior engineer"}},
            },
        )

    # Update interceptor does NOT consume — OpenClaw still gets the RPC
    assert consumed is False
    # Paperclip got the partial PATCH with just the meta fields
    mock_admin.update_agent.assert_awaited_once()
    kwargs = mock_admin.update_agent.await_args.kwargs
    assert kwargs["agent_id"] == "agent-uuid"
    assert kwargs["company_id"] == "co-1"
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Senior engineer"
    # capabilities/icon weren't in the meta block — must NOT be sent
    assert kwargs.get("capabilities") is None
    assert kwargs.get("icon") is None


@pytest.mark.asyncio
async def test_intercept_update_no_meta_is_noop():
    """A normal agents.update without params.paperclip (e.g. model
    change, identity emoji change) doesn't call Paperclip. Returns
    False so OpenClaw still gets the update."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock()

    with patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin):
        consumed = await _intercept_agents_update(
            owner_id="u",
            params={"agentId": "agent-uuid", "model": "claude-sonnet-4-6"},
        )

    assert consumed is False
    mock_admin.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_intercept_update_swallows_paperclip_errors():
    """If Paperclip's PATCH fails, the chat-side update_agent MUST
    NOT propagate the error — the user's OpenClaw update should still
    succeed. Log + drop on the floor (best effort to keep the two
    in sync; OpenClaw is source of truth for runtime state)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(
        side_effect=PaperclipApiError("422", status_code=422, body={"error": "bad role"})
    )
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
    ):
        # Should NOT raise
        consumed = await _intercept_agents_update(
            owner_id="u",
            params={
                "agentId": "a",
                "params": {"paperclip": {"role": "bogus"}},
            },
        )

    assert consumed is False
