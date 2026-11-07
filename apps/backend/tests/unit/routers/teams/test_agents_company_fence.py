"""The per-agent Teams routes 404 an agent outside the caller's workspace company."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from routers.teams import agents as agents_mod
from routers.teams.deps import TeamsContext


def _ctx():
    return TeamsContext(
        user_id="u1",
        org_id="org1",
        owner_id="org1",
        company_id="co1",
        paperclip_user_id="pcu1",
        session_cookie="cookie1",
    )


def _admin(agent):
    fake = MagicMock()
    fake.get_agent = AsyncMock(return_value=agent)
    return fake


@pytest.mark.asyncio
async def test_company_agent_passes_own_agent():
    with patch("routers.teams.agents._admin", return_value=_admin({"id": "a1", "companyId": "co1"})):
        assert await agents_mod._company_agent("a1", ctx=_ctx()) == "a1"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [{"id": "a1", "companyId": "co2"}, {"id": "a1"}, None])
async def test_company_agent_404s_foreign_or_missing(agent):
    with patch("routers.teams.agents._admin", return_value=_admin(agent)):
        with pytest.raises(HTTPException) as exc:
            await agents_mod._company_agent("a1", ctx=_ctx())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_agent_404s_foreign_agent():
    with patch("routers.teams.agents._admin", return_value=_admin({"id": "a1", "companyId": "co2"})):
        with pytest.raises(HTTPException) as exc:
            await agents_mod.get_agent("a1", ctx=_ctx())
    assert exc.value.status_code == 404


def test_every_per_agent_route_is_fenced():
    """Each ``/agents/{agent_id}...`` route either depends on the fence, is get_agent (inline) or is gone (410)."""
    fenced = {
        r.endpoint.__name__
        for r in agents_mod.router.routes
        if "{agent_id}" in getattr(r, "path", "")
        and any(d.call is agents_mod._company_agent for d in r.dependant.dependencies)
    }
    per_agent = {r.endpoint.__name__ for r in agents_mod.router.routes if "{agent_id}" in getattr(r, "path", "")}
    assert per_agent - fenced == {"get_agent", "agent_heartbeat_gone"}
