# apps/backend/tests/unit/routers/teams/test_agents_pause_resume.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_pause_agent_route_calls_admin_pause():
    fake_admin = MagicMock()
    fake_admin.pause_agent = AsyncMock(return_value={"id": "a1", "status": "paused"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.pause_agent("a1", ctx=_ctx())
    fake_admin.pause_agent.assert_awaited_once_with(agent_id="a1", session_cookie="cookie1")
    assert out["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_agent_route_calls_admin_resume():
    fake_admin = MagicMock()
    fake_admin.resume_agent = AsyncMock(return_value={"id": "a1", "status": "idle"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.resume_agent("a1", ctx=_ctx())
    fake_admin.resume_agent.assert_awaited_once_with(agent_id="a1", session_cookie="cookie1")
    assert out["status"] == "idle"


@pytest.mark.asyncio
async def test_pause_resume_redact_adapter_fields():
    """Paperclip echoes the agent record on pause/resume, which can include
    adapterConfig/adapterType (the OpenClaw service token). The routes MUST
    redact them like the GET/PATCH handlers — never leak to the browser."""
    record = {
        "id": "a1",
        "status": "paused",
        "adapterConfig": {"authToken": "SECRET"},
        "adapterType": "openclaw_gateway",
    }
    fake_admin = MagicMock()
    fake_admin.pause_agent = AsyncMock(return_value=dict(record))
    fake_admin.resume_agent = AsyncMock(return_value={**record, "status": "idle"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        paused = await agents_mod.pause_agent("a1", ctx=_ctx())
        resumed = await agents_mod.resume_agent("a1", ctx=_ctx())
    for out in (paused, resumed):
        assert "adapterConfig" not in out
        assert "adapterType" not in out
        assert out["id"] == "a1"
