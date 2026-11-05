"""agent_registry_repo against a moto `tasks` table: global id uniqueness, idempotent create, versioned updates."""

import pytest

from core.repositories import agent_registry_repo as repo

AID = "11111111-1111-4111-8111-111111111111"


def _row(**over):
    return {"agentId": AID, "name": "Engineering", "role": "engineer"} | over


@pytest.mark.asyncio
async def test_put_new_is_unique_globally(tasks_table):
    await repo.put_new("org_a", _row())
    with pytest.raises(repo.AgentConflict):
        await repo.put_new("org_b", _row(name="Other"))
    assert await repo.get("org_b", AID) is None
    assert await repo.owner_of(AID) == "org_a"
    assert (await repo.get("org_a", AID))["name"] == "Engineering"


@pytest.mark.asyncio
async def test_put_new_same_ws_idempotent(tasks_table):
    first, created = await repo.put_new("org_a", _row())
    again, created_again = await repo.put_new("org_a", _row(name="Renamed"))
    assert (created, created_again) == (True, False)
    assert again == first


@pytest.mark.asyncio
async def test_upsert_increments_version(tasks_table):
    await repo.put_new("org_a", _row())
    row = await repo.upsert_fields("org_a", AID, {"name": "Eng", "title": "Lead"})
    assert (row["version"], row["name"], row["title"]) == (2, "Eng", "Lead")
    assert (await repo.mark_deleted("org_a", AID))["status"] == "deleted"
    with pytest.raises(ValueError):
        await repo.upsert_fields("org_a", AID, {"version": 9})
