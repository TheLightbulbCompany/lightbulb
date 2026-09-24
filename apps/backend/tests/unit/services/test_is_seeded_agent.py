# apps/backend/tests/unit/services/test_is_seeded_agent.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_openclaw_pairing as pop


# --- get_seeded_agent_ids: the persisted-id source of truth ---


@pytest.mark.asyncio
async def test_get_seeded_ids_reads_the_workspace_row():
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=MagicMock(seeded_agent_ids=["a1", "a3"]))
    ids = await pop.get_seeded_agent_ids(owner_id="org_x", repo=repo)
    assert ids == {"a1", "a3"}
    repo.get_workspace.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_get_seeded_ids_workspace_without_ids():
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=MagicMock(seeded_agent_ids=None))
    assert await pop.get_seeded_agent_ids(owner_id="org_x", repo=repo) == set()


@pytest.mark.asyncio
async def test_get_seeded_ids_swallows_errors():
    repo = MagicMock()
    repo.get_workspace = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await pop.get_seeded_agent_ids(owner_id="org_x", repo=repo) == set()


# --- is_seeded_agent: membership against the persisted set (fails CLOSED) ---


@pytest.mark.asyncio
async def test_seeded_id_is_seeded():
    with patch.object(pop, "_resolve_seeded_agent_ids", new=AsyncMock(return_value={"a1", "a3"})):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a1") is True
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a3") is True


@pytest.mark.asyncio
async def test_user_created_ceo_is_not_seeded():
    # A user agent that uses role "ceo" / name "Atlas" must NOT be flagged —
    # only persisted seeded ids count.
    with patch.object(pop, "_resolve_seeded_agent_ids", new=AsyncMock(return_value={"a1"})):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a2") is False


@pytest.mark.asyncio
async def test_unknown_agent_not_seeded():
    with patch.object(pop, "_resolve_seeded_agent_ids", new=AsyncMock(return_value=set())):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="ghost") is False


@pytest.mark.asyncio
async def test_is_seeded_fails_closed_on_lookup_error():
    # A DDB outage must NOT let a seeded agent be deleted: the delete guard
    # treats a lookup failure as seeded (blocks the delete).
    with patch.object(pop, "_resolve_seeded_agent_ids", new=AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="anything") is True
