from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc
from core.services.messaging import ApiProblem


def _registry(monkeypatch, roster=({"id": "a1", "role": "general"},)):
    from core.services import agent_registry

    update = AsyncMock(return_value={})
    monkeypatch.setattr(agent_registry, "roster", AsyncMock(return_value=list(roster)))
    monkeypatch.setattr(agent_registry, "update", update)
    return update


@pytest.mark.asyncio
async def test_name_only_update_is_written_to_the_registry(monkeypatch):
    update = _registry(monkeypatch)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Ada"})

    update.assert_awaited_once_with("org_1", "a1", {"name": "Ada"})


@pytest.mark.asyncio
async def test_name_and_existing_metadata_share_one_write(monkeypatch):
    update = _registry(monkeypatch)
    params = {
        "agentId": "a1",
        "name": "Ada",
        "params": {
            "paperclip": {
                "role": "researcher",
                "title": "Research lead",
                "capabilities": "Finds evidence",
                "icon": "search",
            }
        },
    }

    await runtime_rpc._mirror_agent_update("org_1", params)

    update.assert_awaited_once_with(
        "org_1",
        "a1",
        {"name": "Ada", "role": "researcher", "title": "Research lead", "persona": "Finds evidence", "icon": "search"},
    )
    assert params == {"agentId": "a1", "name": "Ada"}


@pytest.mark.asyncio
async def test_a_registry_outage_refuses_instead_of_answering_ok(monkeypatch):
    """The name was written nowhere; that is a lost edit, not ok."""
    from core.services import agent_registry

    monkeypatch.setattr(agent_registry, "roster", AsyncMock(side_effect=agent_registry.RegistryUnavailable("down")))

    with pytest.raises(ApiProblem) as exc:
        await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Ada"})

    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_noop_update_writes_nothing(monkeypatch):
    update = _registry(monkeypatch)

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "provider/model"})

    update.assert_not_awaited()


class _CountingName(str):
    strip_calls = 0

    def strip(self, chars=None):
        type(self).strip_calls += 1
        return super().strip(chars)


@pytest.mark.asyncio
async def test_execute_normalizes_agent_name_once_before_the_write(monkeypatch):
    """Once, at the RPC boundary -- and never on the caller's own dict. Normalising per writer
    is how a rename lands under two different names."""
    _CountingName.strip_calls = 0
    mirror = AsyncMock()
    monkeypatch.setattr(runtime_rpc, "_mirror_agent_update", mirror)
    supplied = {"agentId": "a1", "name": _CountingName("  Ada  ")}

    await runtime_rpc.execute_sync(
        owner_id="org_1",
        user_id="user_1",
        request_id="request",
        method="agents.update",
        params=supplied,
    )

    assert _CountingName.strip_calls == 1
    assert mirror.await_args.args[1]["name"] == "Ada"
    assert supplied["name"] == "  Ada  "


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   "])
async def test_execute_rejects_empty_agent_name_at_rpc_boundary(monkeypatch, name):
    mirror = AsyncMock()
    monkeypatch.setattr(runtime_rpc, "_mirror_agent_update", mirror)
    call_mock = AsyncMock(return_value={})
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", call_mock)

    with pytest.raises(ApiProblem) as exc_info:
        await runtime_rpc.execute_sync(
            owner_id="org_1",
            user_id="user_1",
            request_id="request",
            method="agents.update",
            params={"agentId": "a1", "name": name},
        )

    assert exc_info.value.error.code == "INVALID_REQUEST"
    mirror.assert_not_awaited()
    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_root_ceo_cannot_be_demoted(monkeypatch):
    update = _registry(monkeypatch, [{"id": "a1", "role": "ceo", "reportsTo": None}])

    with pytest.raises(RuntimeError, match="coordinator cannot be deleted"):
        await runtime_rpc._mirror_agent_update(
            "org_1",
            {"agentId": "a1", "params": {"paperclip": {"role": "general"}}},
        )

    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_root_ceo_can_be_renamed(monkeypatch):
    update = _registry(monkeypatch, [{"id": "a1", "role": "ceo", "reportsTo": None}])

    await runtime_rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed"})

    update.assert_awaited_once()
