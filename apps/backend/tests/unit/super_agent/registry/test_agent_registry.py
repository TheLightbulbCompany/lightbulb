"""agent_registry service: roster() matches today's load_agent_roster on the keys consumers read (spec 03 §2.1)."""

import pytest

from core.repositories import agent_registry_repo as repo
from core.services import agent_registry

# The keys roster consumers read (wave1-R Global Constraints), icon/appearance included.
KEYS = ("id", "name", "role", "title", "status", "reportsTo", "capabilities", "icon", "appearance", "companyId")


async def _seed(ws, agents):
    for a in agents:
        if a["status"] != "terminated":
            await repo.put_new(ws, agent_registry.row_from_paperclip(a))


def _project(row):
    out = {k: row.get(k) for k in KEYS}
    out["status"] = "paused" if row.get("status") == "paused" else "active"
    meta = row.get("metadata") or {}
    out["metadata"] = {k: meta[k] for k in ("isol8_listing_id", "isol8_listing_slug") if meta.get(k)}
    return out


@pytest.mark.asyncio
async def test_roster_excludes_deleted(tasks_table, paperclip_roster):
    await _seed("org_a", paperclip_roster)
    gone = paperclip_roster[1]["id"]
    await repo.mark_deleted("org_a", gone)
    ids = [r["id"] for r in await agent_registry.roster("org_a")]
    assert gone not in ids and ids == sorted(ids) and len(ids) == 6


@pytest.mark.asyncio
async def test_registry_unavailable_on_client_error(ddb):
    with pytest.raises(agent_registry.RegistryUnavailable):
        await agent_registry.roster("org_a")
    with pytest.raises(agent_registry.RegistryUnavailable):
        await agent_registry.owner_of("11111111-1111-4111-8111-111111111111")
