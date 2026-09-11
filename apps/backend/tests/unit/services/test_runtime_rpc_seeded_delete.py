"""The runtime RPC delete entry point delegates to the shared lifecycle."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.messaging import ApiProblem
from core.services.paperclip_agent_lifecycle import AgentLifecycleConflict


@pytest.mark.asyncio
async def test_final_root_conflict_is_stable_409_before_runtime_lookup(monkeypatch):
    delete_everywhere = AsyncMock(
        side_effect=AgentLifecycleConflict("The final root CEO cannot be deleted or demoted.")
    )
    monkeypatch.setattr(rpc, "delete_agent_everywhere", delete_everywhere)
    running = AsyncMock()
    monkeypatch.setattr(rpc, "_running_container", running)
    pool_factory = MagicMock()
    monkeypatch.setattr(rpc, "get_gateway_pool", pool_factory)

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="o1",
            user_id="user_1",
            request_id="r1",
            method="agents.delete",
            params={"agentId": "friday-uuid"},
        )

    err = exc_info.value
    assert err.error.code == "AGENT_LIFECYCLE_CONFLICT"
    assert err.status_code == 409
    assert "final root CEO" in err.error.message
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="friday-uuid")
    running.assert_not_awaited()
    pool_factory.assert_not_called()


@pytest.mark.asyncio
async def test_specialist_delete_delegates_to_shared_lifecycle(monkeypatch):
    delete_everywhere = AsyncMock(return_value={"deleted": True, "agent_id": "bob-uuid"})
    monkeypatch.setattr(rpc, "delete_agent_everywhere", delete_everywhere)
    running = AsyncMock()
    monkeypatch.setattr(rpc, "_running_container", running)

    result = await rpc.execute_sync(
        owner_id="o1",
        user_id="user_1",
        request_id="r1",
        method="agents.delete",
        params={"agentId": "bob-uuid"},
    )

    assert result == {"deleted": True, "agent_id": "bob-uuid"}
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="bob-uuid")
    running.assert_not_awaited()
