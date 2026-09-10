"""core.services.runtime_rpc._create_agent registers the new agent's
Lightbulb channel account + binding after pairing it with OpenClaw.

build_openclaw_config_dict only derives channels.lightbulb.accounts/bindings
when the whole config is (re)written (provision, reapply, image update). An
agent hired via the chat-intercepted ``agents.create`` RPC never goes through
that path, so without this call the new agent has no inbound route on the
``lightbulb`` channel — see core.services.config_patcher
.register_lightbulb_agent_account.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import runtime_rpc as rpc


@pytest.mark.asyncio
async def test_create_agent_registers_lightbulb_channel_account(monkeypatch):
    monkeypatch.setattr(
        rpc,
        "create_paperclip_for_chat",
        AsyncMock(return_value={"id": "agent_123", "name": "Bob"}),
    )
    monkeypatch.setattr("core.services.agent_budget.is_at_agent_budget", AsyncMock(return_value=False))
    monkeypatch.setattr(rpc, "get_gateway_pool", lambda: MagicMock())
    pair_mock = AsyncMock()
    monkeypatch.setattr(rpc, "pair_with_openclaw", pair_mock)
    monkeypatch.setattr(rpc, "ensure_operator_device_paired", AsyncMock())
    register_mock = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.register_lightbulb_agent_account", register_mock)

    result = await rpc._create_agent("owner_1", "user_1", {"name": "Bob"}, "10.0.0.1", "tok")

    assert result["agentId"] == "agent_123"
    pair_mock.assert_awaited_once()
    register_mock.assert_awaited_once_with("owner_1", "agent_123")
