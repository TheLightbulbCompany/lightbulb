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
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co-1"})

    rpc_params = {
        "agentId": "agent-uuid",
        "name": "Renamed",
        "params": {"paperclip": {"role": "engineer", "title": "Senior engineer"}},
    }

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
            params=rpc_params,
        )

    # Update interceptor does NOT consume — OpenClaw still gets the RPC
    assert consumed is False
    # Paperclip got the partial PATCH with just the meta fields
    mock_admin.update_agent.assert_awaited_once()
    kwargs = mock_admin.update_agent.await_args.kwargs
    assert kwargs["agent_id"] == "agent-uuid"
    # Route is PATCH /api/agents/:id — not company-scoped, so the
    # interceptor must not thread a companyId into the update call.
    assert "company_id" not in kwargs
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Senior engineer"
    # capabilities/icon weren't in the meta block — must NOT be sent
    assert kwargs.get("capabilities") is None
    assert kwargs.get("icon") is None
    # Codex P1: the Paperclip-only ``params`` block MUST be stripped
    # in place so the dispatcher's forward to OpenClaw doesn't 422
    # against ``additionalProperties: false``.
    assert "params" not in rpc_params
    assert rpc_params["agentId"] == "agent-uuid"
    assert rpc_params["name"] == "Renamed"


@pytest.mark.asyncio
async def test_intercept_update_skips_cross_company_agent():
    """The Paperclip PATCH hits the unscoped /api/agents/{id} with the
    bootstrap admin cookie, so the interceptor must confirm the target
    belongs to the owner's company first. A client-supplied agent id
    from another company must NOT be PATCHed."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "foreign"})
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "other-co"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))

    rpc_params = {
        "agentId": "foreign-agent",
        "params": {"paperclip": {"role": "engineer"}},
    }

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
            params=rpc_params,
        )

    assert consumed is False
    # Ownership check failed → the foreign agent is never PATCHed.
    mock_admin.update_agent.assert_not_awaited()
    # The Paperclip-only params block is still stripped so the OpenClaw
    # forward stays schema-valid.
    assert "params" not in rpc_params


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
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co"})

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


@pytest.mark.asyncio
async def test_intercept_update_strips_paperclip_params_so_openclaw_doesnt_422():
    """Codex P1: OpenClaw's agents.update schema is strict
    (additionalProperties: false). The interceptor MUST mutate the
    params dict to remove the inner ``params`` block before returning,
    so the dispatcher's forward to OpenClaw only carries
    schema-allowed fields."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "agent-uuid"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co-1"})

    rpc_params = {
        "agentId": "agent-uuid",
        "name": "Ray",  # OpenClaw-allowed, MUST survive
        "params": {  # Paperclip-only, MUST be stripped
            "paperclip": {"role": "engineer", "title": "Backend dev"}
        },
    }

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="sess=c"),
        ),
    ):
        await _intercept_agents_update(owner_id="user_owner", params=rpc_params)

    # The dict passed in was mutated: paperclip ``params`` is gone,
    # OpenClaw-allowed fields survive.
    assert "params" not in rpc_params
    assert rpc_params["agentId"] == "agent-uuid"
    assert rpc_params["name"] == "Ray"


@pytest.mark.asyncio
async def test_intercept_update_strips_params_even_when_paperclip_mirror_fails():
    """The strip happens regardless of whether Paperclip's PATCH
    succeeded — otherwise an upstream 422/transport blip would also
    break the OpenClaw-side update."""
    from core.services.paperclip_admin_client import PaperclipApiError

    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(side_effect=PaperclipApiError("422", status_code=422, body={"error": "bad"}))
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co"})

    rpc_params = {
        "agentId": "a",
        "params": {"paperclip": {"role": "bogus"}},
    }

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
    ):
        await _intercept_agents_update(owner_id="u", params=rpc_params)

    assert "params" not in rpc_params
    assert rpc_params["agentId"] == "a"


@pytest.mark.asyncio
async def test_intercept_create_persists_paperclip_meta_to_openclaw_json():
    """After pair_with_openclaw, the interceptor writes params.paperclip
    into the buyer's openclaw.json via config_patcher — so marketplace
    publish slice extraction picks up the meta later."""
    create_call = AsyncMock(return_value={"id": "agent-uuid", "name": "Ray"})
    apply_meta = AsyncMock(return_value=None)
    mock_pair = AsyncMock(return_value=None)

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=mock_pair),
        patch("routers.websocket_chat.get_gateway_pool", return_value=MagicMock()),
        patch("routers.websocket_chat.get_management_api_client", new=AsyncMock(return_value=MagicMock())),
        patch("core.services.config_patcher.apply_paperclip_meta", new=apply_meta),
    ):
        await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="u",
            owner_id="owner",
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

    apply_meta.assert_awaited_once()
    kwargs = apply_meta.await_args.kwargs
    assert kwargs["agent_id"] == "agent-uuid"
    assert kwargs["meta"]["role"] == "engineer"
    assert kwargs["meta"]["title"] == "Backend dev"
    assert kwargs["meta"]["capabilities"] == "Writes Python."


@pytest.mark.asyncio
async def test_intercept_create_skips_efs_write_when_no_meta():
    """No params.paperclip in the RPC → no EFS write."""
    create_call = AsyncMock(return_value={"id": "agent-uuid"})
    apply_meta = AsyncMock(return_value=None)

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=AsyncMock()),
        patch("routers.websocket_chat.get_gateway_pool", return_value=MagicMock()),
        patch("routers.websocket_chat.get_management_api_client", new=AsyncMock(return_value=MagicMock())),
        patch("core.services.config_patcher.apply_paperclip_meta", new=apply_meta),
    ):
        await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="u",
            owner_id="owner",
            params={"name": "Ray"},
            ip="10.0.0.1",
            token="tok",
        )

    apply_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_intercept_update_persists_paperclip_meta_to_openclaw_json():
    """Update path also writes params.paperclip to the file, alongside
    the Paperclip PATCH."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "agent-uuid"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co"})
    apply_meta = AsyncMock(return_value=None)

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.config_patcher.apply_paperclip_meta", new=apply_meta),
    ):
        await _intercept_agents_update(
            owner_id="owner",
            params={
                "agentId": "agent-uuid",
                "params": {"paperclip": {"role": "engineer", "title": "Senior"}},
            },
        )

    apply_meta.assert_awaited_once()
    kwargs = apply_meta.await_args.kwargs
    assert kwargs["agent_id"] == "agent-uuid"
    assert kwargs["meta"]["role"] == "engineer"
    assert kwargs["meta"]["title"] == "Senior"


@pytest.mark.asyncio
async def test_intercept_update_swallows_efs_write_errors():
    """If config_patcher fails, the update interceptor must still
    return without raising — the chat-side update still forwards to
    OpenClaw for its allowed fields."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "a"})
    mock_admin.get_agent = AsyncMock(return_value={"companyId": "co"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
    apply_meta = AsyncMock(side_effect=RuntimeError("EFS write failed"))

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.config_patcher.apply_paperclip_meta", new=apply_meta),
    ):
        # Should NOT raise
        result = await _intercept_agents_update(
            owner_id="u",
            params={"agentId": "a", "params": {"paperclip": {"role": "engineer"}}},
        )

    assert result is False
