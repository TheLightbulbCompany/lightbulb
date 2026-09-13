"""core.services.runtime_rpc._create_agent ends up registering the new
agent's Lightbulb channel account + binding, via pair_with_openclaw — the
single chokepoint every agent-creation path routes through (see
core.services.paperclip_openclaw_pairing.pair_with_openclaw's docstring).

build_openclaw_config_dict only derives channels.lightbulb.accounts/bindings
when the whole config is (re)written (provision, reapply, image update). An
agent hired via the chat-intercepted ``agents.create`` RPC never goes through
that path, so without this the new agent has no inbound route on the
``lightbulb`` channel.

This test runs the real ``pair_with_openclaw`` (not mocked) so the
registration call inside it actually fires — only its gateway RPCs and the
registration helper itself are stubbed.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc as rpc


@pytest.mark.asyncio
async def test_create_agent_registers_lightbulb_channel_account(monkeypatch):
    monkeypatch.setattr(
        rpc,
        "create_paperclip_for_chat",
        AsyncMock(return_value={"id": "agent_123", "name": "Bob"}),
    )
    count_agents = AsyncMock(return_value=11)
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
        count_agents,
    )
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )
    call = AsyncMock(return_value={"ok": True, "agentId": "agent_123"})
    monkeypatch.setattr("core.services.paperclip_openclaw_pairing.container_rpc.call", call)
    monkeypatch.setattr(rpc, "ensure_operator_device_paired", AsyncMock())
    register_mock = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.register_lightbulb_agent_account", register_mock)

    result = await rpc._create_agent("owner_1", "user_1", {"name": "Bob"}, "10.0.0.1", "tok")

    assert result["agentId"] == "agent_123"
    assert call.await_count == 2
    register_mock.assert_awaited_once_with("owner_1", "agent_123")
    count_agents.assert_not_awaited()
    assert "idempotency_replay_only" not in rpc.create_paperclip_for_chat.await_args.kwargs
