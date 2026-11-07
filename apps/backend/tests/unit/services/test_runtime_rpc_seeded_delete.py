"""The runtime RPC delete entry point delegates to the shared lifecycle."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.messaging import ApiProblem
from core.services.agent_lifecycle import AgentLifecycleConflict


@pytest.mark.asyncio
async def test_final_root_conflict_is_stable_409_before_runtime_lookup(monkeypatch):
    delete_everywhere = AsyncMock(
        side_effect=AgentLifecycleConflict("Lightbulb's coordinator cannot be deleted or demoted.")
    )
    monkeypatch.setattr(rpc, "delete_agent_everywhere", delete_everywhere)
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

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
    assert "coordinator cannot be deleted" in err.error.message
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="friday-uuid")
    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_specialist_delete_delegates_to_shared_lifecycle(monkeypatch):
    delete_everywhere = AsyncMock(return_value={"deleted": True, "agent_id": "bob-uuid"})
    monkeypatch.setattr(rpc, "delete_agent_everywhere", delete_everywhere)
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    result = await rpc.execute_sync(
        owner_id="o1",
        user_id="user_1",
        request_id="r1",
        method="agents.delete",
        params={"agentId": "bob-uuid"},
    )

    assert result == {"deleted": True, "agent_id": "bob-uuid"}
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="bob-uuid")
    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_profile_delete_fault_is_a_clean_rpc_error_not_a_500(monkeypatch):
    """Q9a row #3 (task-9-scan.md): the durable record of an agent is its S3 profile, so the
    delete's transport fault is an S3 one now (it was an EFS config-lock error). It must still
    surface as a clean, retryable RPC error -- not an unhandled 500."""
    import botocore.exceptions

    delete_everywhere = AsyncMock(
        side_effect=botocore.exceptions.ClientError(
            {"Error": {"Code": "SlowDown", "Message": "please retry"}}, "DeleteObject"
        )
    )
    monkeypatch.setattr(rpc, "delete_agent_everywhere", delete_everywhere)
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="o1",
            user_id="user_1",
            request_id="r1",
            method="agents.delete",
            params={"agentId": "bob-uuid"},
        )

    err = exc_info.value
    assert err.error.code == "RUNTIME_UNAVAILABLE"
    assert err.status_code == 503
    call_mock.assert_not_awaited()
