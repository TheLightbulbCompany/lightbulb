# apps/backend/tests/unit/services/test_is_seeded_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import paperclip_openclaw_pairing as pop


@pytest.mark.asyncio
async def test_friday_ceo_is_seeded():
    agents = [{"id": "a1", "name": "Friday", "role": "ceo"}, {"id": "a2", "name": "Bob", "role": "engineer"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a1") is True


@pytest.mark.asyncio
async def test_atlas_by_name_is_seeded():
    agents = [{"id": "a3", "name": "Atlas", "role": "general"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a3") is True


@pytest.mark.asyncio
async def test_regular_agent_not_seeded():
    agents = [{"id": "a2", "name": "Bob", "role": "engineer"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a2") is False


@pytest.mark.asyncio
async def test_unknown_agent_not_seeded():
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=[])):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="ghost") is False
