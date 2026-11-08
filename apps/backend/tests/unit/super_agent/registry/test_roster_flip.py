"""load_agent_roster reads the AGENT# registry for super-agent workspaces (wave1-R Task 5, review focus 2 and 3)."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import agent_registry_repo as repo
from core.services import agent_registry
from core.services import messaging
from tests.unit.super_agent.registry.conftest import registry_row
from core.services.messaging import ApiProblem

WS = "org_a"


async def _seed(roster):
    for a in roster:
        if a["status"] != "terminated":
            await repo.put_new(WS, registry_row(a))


@pytest.mark.asyncio
async def test_flag_on_reads_registry_not_paperclip(tasks_table, paperclip_roster):
    await _seed(paperclip_roster)
    roster = await messaging.load_agent_roster(WS)
    registry = await agent_registry.roster(WS)
    front = next((r["id"] for r in registry if r.get("role") == "ceo" and r.get("status") == "active"), None)
    # Same rows, except the front agent is named "Lightbulb" (contracts-v2 §9).
    assert roster == [{**r, "name": "Lightbulb"} if r["id"] == front else r for r in registry]
    assert [r["id"] for r in roster] == sorted(r["id"] for r in roster)


@pytest.mark.asyncio
async def test_flag_on_names_the_front_agent_lightbulb(tasks_table, paperclip_roster):
    await _seed(paperclip_roster)
    registry = await agent_registry.roster(WS)
    ceo = next(r for r in registry if r.get("role") == "ceo")
    with (
        patch.object(agent_registry, "roster", AsyncMock(return_value=[{**r, "status": "active"} for r in registry])),
    ):
        roster = await messaging.load_agent_roster(WS)
    names = {r["id"]: r["name"] for r in roster}
    assert names[ceo["id"]] == "Lightbulb"
    assert all(n != "Lightbulb" for i, n in names.items() if i != ceo["id"])


@pytest.mark.asyncio
async def test_registry_unavailable_maps_to_runtime_unavailable(ddb):
    with pytest.raises(ApiProblem) as exc:
        await messaging.load_agent_roster(WS)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc.value.error.retryable is True


@pytest.mark.asyncio
async def test_registry_bad_id_maps_to_internal_error(tasks_table):
    await repo.put_new(WS, {"agentId": "not-a-uuid", "name": "Bad"})
    with pytest.raises(ApiProblem) as exc:
        await messaging.load_agent_roster(WS)
    assert exc.value.error.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_create_then_roster_registry_mode(tasks_table):
    """An agent created through the registry shows on the next roster read."""
    agent = await agent_registry.create(WS, name="Ops", role="general")
    roster = await messaging.load_agent_roster(WS)
    assert [(r["id"], r["name"]) for r in roster] == [(agent["id"], "Ops")]
