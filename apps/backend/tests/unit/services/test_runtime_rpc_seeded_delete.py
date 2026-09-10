"""Friday/Atlas (seeded agents) cannot be deleted through the runtime RPC
compatibility endpoint.

Moved (with the rest of the guard policy) from the retired WS chat socket's
``_guard_seeded_delete``. The check now lives inline in
``core.services.runtime_rpc.execute_sync``'s ``agents.delete`` branch, ahead
of ``prepare_paperclip_agent_delete`` and the gateway pool -- a seeded delete
must never reach Paperclip or the container.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.messaging import ApiProblem
from core.services import runtime_rpc as rpc


def _mock_running_container(monkeypatch) -> None:
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, "10.0.0.1"))
    monkeypatch.setattr(rpc, "get_ecs_manager", lambda: ecs)


@pytest.mark.asyncio
async def test_seeded_agent_delete_is_rejected_before_forward(monkeypatch):
    _mock_running_container(monkeypatch)
    monkeypatch.setattr(rpc, "is_seeded_agent", AsyncMock(return_value=True))
    prepare_mock = AsyncMock()
    monkeypatch.setattr(rpc, "prepare_paperclip_agent_delete", prepare_mock)
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
    assert err.error.code == "INVALID_REQUEST"
    assert "cannot be deleted" in err.error.message.lower()
    prepare_mock.assert_not_awaited()
    pool_factory.assert_not_called()


@pytest.mark.asyncio
async def test_non_seeded_agent_delete_is_not_blocked_by_this_guard(monkeypatch):
    _mock_running_container(monkeypatch)
    monkeypatch.setattr(rpc, "is_seeded_agent", AsyncMock(return_value=False))
    prepare_mock = AsyncMock(return_value={})
    monkeypatch.setattr(rpc, "prepare_paperclip_agent_delete", prepare_mock)
    delete_retry = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(rpc, "delete_runtime_agent_with_lock_retry", delete_retry)
    monkeypatch.setattr(rpc, "_cleanup_deleted_agent", AsyncMock())
    monkeypatch.setattr(rpc, "get_gateway_pool", lambda: MagicMock())

    result = await rpc.execute_sync(
        owner_id="o1",
        user_id="user_1",
        request_id="r1",
        method="agents.delete",
        params={"agentId": "bob-uuid"},
    )

    assert result == {"ok": True}
    prepare_mock.assert_awaited_once()
    delete_retry.assert_awaited_once()
