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

    pc_create_mock = AsyncMock(return_value={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"})
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
async def test_chat_paperclip_create_mints_service_token_with_owner_id(monkeypatch):
    """Codex P1 regression: when an org member triggers /chat agents.create,
    the synthesized adapterConfig.authToken must be a JWT minted with
    sub=owner_id (the org), NOT sub=member_id. The WS authorizer treats
    sub as the auth principal and puts ``orgId=""`` into the context, so
    a member-keyed service token routes future Paperclip wakes as the
    member's personal owner — and the agent could never reach the org's
    container it was just paired into.

    sessionKey (the adapter's user_id field) is still the member id —
    that's the per-creator manual-wake fallback session, not auth."""

    # Drive _paperclip_create_for_chat directly so we can inspect the
    # service_token.mint call args without needing a full ECS / WS stack.
    mint_mock = MagicMock(return_value="svc.tok.minted")
    monkeypatch.setattr(
        "core.services.service_token.mint",
        mint_mock,
    )

    # Intercept synthesize_openclaw_adapter to capture the user_id arg too.
    synth_mock = MagicMock(return_value={"type": "openclaw_gateway"})
    monkeypatch.setattr(
        "core.services.paperclip_adapter_config.synthesize_openclaw_adapter",
        synth_mock,
    )

    # Stub Paperclip lookup so we never touch DDB / HTTP.
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing._resolve_company_id_for_owner",
        AsyncMock(return_value="co_xyz"),
    )

    # Stub the admin session cookie + admin client so the real httpx client
    # never opens.
    monkeypatch.setattr(
        "core.services.paperclip_admin_session.get_admin_session_cookie",
        AsyncMock(return_value="cookie"),
    )

    fake_admin = MagicMock()
    fake_admin.create_agent = AsyncMock(
        return_value={"id": "pc-uuid", "name": "X"},
    )
    monkeypatch.setattr(
        "core.services.paperclip_admin_client.PaperclipAdminClient",
        lambda http_client: fake_admin,
    )

    # Avoid actually constructing an httpx.AsyncClient (it would try to
    # bind to PAPERCLIP_INTERNAL_URL). The real client is closed in a
    # ``finally`` block, so the stub needs an awaitable ``aclose``.
    fake_http = MagicMock()
    fake_http.aclose = AsyncMock()
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: fake_http,
    )

    from routers.websocket_chat import _paperclip_create_for_chat

    await _paperclip_create_for_chat(
        creator_user_id="user_member_b",
        owner_id="org_xyz",
        name="Code Reviewer",
        role="engineer",
        model=None,
    )

    # P1: token is minted under the OWNER's id, not the member's.
    mint_mock.assert_called_once_with("org_xyz")

    # The sessionKey (adapter user_id) still uses the member id — that's
    # the per-creator manual-wake fallback, not the auth principal.
    synth_mock.assert_called_once()
    assert synth_mock.call_args.kwargs["user_id"] == "user_member_b"
    assert synth_mock.call_args.kwargs["service_token"] == "svc.tok.minted"


@pytest.mark.asyncio
async def test_chat_agents_create_with_no_name_falls_through_to_gateway(monkeypatch):
    """If the RPC has no name, don't try to interpret it — let OpenClaw's
    own validator reject. We don't duplicate validation."""
    pair_mock = AsyncMock()
    pc_create_mock = AsyncMock()
    monkeypatch.setattr(
        "routers.websocket_chat._paperclip_create_for_chat",
        pc_create_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.pair_with_openclaw",
        pair_mock,
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


@pytest.mark.asyncio
async def test_chat_agents_create_paperclip_failure_responds_ok_false(monkeypatch):
    """If _paperclip_create_for_chat raises, the dispatcher's safety
    try/except must:
    1. Send an ok:false response frame back to the WS client (so the
       frontend isn't left hanging).
    2. NOT forward the original agents.create RPC to the gateway —
       otherwise we'd leak a phantom agent without the Paperclip pairing.
    3. NOT call pair_with_openclaw — there is no Paperclip agent to pair.

    Drives _process_rpc_background end-to-end so the safety net at
    routers/websocket_chat.py:685-704 is covered."""
    pc_create_mock = AsyncMock(side_effect=RuntimeError("paperclip is down"))
    pair_mock = AsyncMock()
    pool_mock = MagicMock()
    pool_mock.send_rpc = AsyncMock()

    monkeypatch.setattr(
        "routers.websocket_chat._paperclip_create_for_chat",
        pc_create_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.get_gateway_pool",
        MagicMock(return_value=pool_mock),
    )

    ecs_mock = MagicMock()
    ecs_mock.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "gw_token"}, "10.0.0.5"),
    )
    monkeypatch.setattr(
        "routers.websocket_chat.get_ecs_manager",
        MagicMock(return_value=ecs_mock),
    )

    mgmt_mock = MagicMock()
    mgmt_mock.send_message = MagicMock(return_value=True)
    monkeypatch.setattr(
        "routers.websocket_chat.get_management_api_client",
        AsyncMock(return_value=mgmt_mock),
    )

    from routers.websocket_chat import _process_rpc_background

    await _process_rpc_background(
        connection_id="conn-fail-1",
        user_id="user_a",
        owner_id="user_a",
        req_id="req-fail-1",
        method="agents.create",
        params={"name": "Reviewer", "role": "engineer"},
    )

    # Paperclip create was attempted; pair was never reached.
    pc_create_mock.assert_awaited_once()
    pair_mock.assert_not_awaited()

    # Original RPC was NOT forwarded to the gateway — that would leak a
    # phantom agent.
    pool_mock.send_rpc.assert_not_awaited()

    # ok:false response was emitted with the failure surfaced as an error.
    mgmt_mock.send_message.assert_called_once()
    target_conn, frame = mgmt_mock.send_message.call_args.args
    assert target_conn == "conn-fail-1"
    assert frame["type"] == "res"
    assert frame["id"] == "req-fail-1"
    assert frame["ok"] is False
    assert "error" in frame
    assert "paperclip is down" in frame["error"]["message"]


@pytest.mark.asyncio
async def test_chat_agents_create_pair_failure_responds_ok_false(monkeypatch):
    """If pair_with_openclaw raises after Paperclip created the agent,
    the dispatcher's safety try/except must still respond ok:false and
    must NOT forward the original RPC to the gateway. The Paperclip
    agent is left orphan-paired (no OpenClaw twin) — that cleanup is a
    separate concern; this test only pins the WS-contract behaviour."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pc_create_mock = AsyncMock(
        return_value={"id": pc_uuid, "name": "Reviewer", "role": "engineer"},
    )
    pair_mock = AsyncMock(side_effect=RuntimeError("gateway timeout"))
    pool_mock = MagicMock()
    pool_mock.send_rpc = AsyncMock()

    monkeypatch.setattr(
        "routers.websocket_chat._paperclip_create_for_chat",
        pc_create_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.websocket_chat.get_gateway_pool",
        MagicMock(return_value=pool_mock),
    )

    ecs_mock = MagicMock()
    ecs_mock.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "gw_token"}, "10.0.0.5"),
    )
    monkeypatch.setattr(
        "routers.websocket_chat.get_ecs_manager",
        MagicMock(return_value=ecs_mock),
    )

    mgmt_mock = MagicMock()
    mgmt_mock.send_message = MagicMock(return_value=True)
    monkeypatch.setattr(
        "routers.websocket_chat.get_management_api_client",
        AsyncMock(return_value=mgmt_mock),
    )

    from routers.websocket_chat import _process_rpc_background

    await _process_rpc_background(
        connection_id="conn-fail-2",
        user_id="user_a",
        owner_id="user_a",
        req_id="req-fail-2",
        method="agents.create",
        params={"name": "Reviewer", "role": "engineer"},
    )

    # Both inner steps were attempted; pairing raised.
    pc_create_mock.assert_awaited_once()
    pair_mock.assert_awaited_once()

    # Original RPC was NOT forwarded to the gateway.
    pool_mock.send_rpc.assert_not_awaited()

    # ok:false response was emitted with the failure surfaced as an error.
    mgmt_mock.send_message.assert_called_once()
    target_conn, frame = mgmt_mock.send_message.call_args.args
    assert target_conn == "conn-fail-2"
    assert frame["type"] == "res"
    assert frame["id"] == "req-fail-2"
    assert frame["ok"] is False
    assert "error" in frame
    assert "gateway timeout" in frame["error"]["message"]
