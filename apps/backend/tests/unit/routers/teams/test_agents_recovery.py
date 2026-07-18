# apps/backend/tests/unit/routers/teams/test_agents_recovery.py
"""Recovery ops for a wedged agent — reset-session + clear-error (A7).

Mirrors ``test_agents_pause_resume.py``: the BFF proxy routes forward to the
admin client with the caller's session cookie and redact adapter fields on the
way back (Paperclip echoes the agent record on clear-error and may echo it on
reset-session). Neither op dispatches an inference run, so — unlike wakeup —
there is no trial-runway gate here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.teams import agents as agents_mod
from routers.teams.deps import TeamsContext
from routers.teams.schemas import ResetAgentSessionBody


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
async def test_reset_session_route_calls_admin_with_camel_body():
    """The reset-session route forwards a camelCase, none-dropped body
    (``taskKey``) to the admin client, threading the session cookie."""
    fake_admin = MagicMock()
    fake_admin.reset_agent_session = AsyncMock(return_value={"ok": True})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.reset_agent_session(
            "a1",
            body=ResetAgentSessionBody(task_key="task-42"),
            ctx=_ctx(),
        )
    fake_admin.reset_agent_session.assert_awaited_once_with(
        agent_id="a1",
        body={"taskKey": "task-42"},
        session_cookie="cookie1",
    )
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_reset_session_route_empty_body_resets_all_sessions():
    """A bare reset (no taskKey) is valid and forwards an empty body — the
    recovery button clears all runtime sessions for a wedged agent."""
    fake_admin = MagicMock()
    fake_admin.reset_agent_session = AsyncMock(return_value={"state": "reset"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.reset_agent_session(
            "a1",
            body=ResetAgentSessionBody(),
            ctx=_ctx(),
        )
    fake_admin.reset_agent_session.assert_awaited_once_with(
        agent_id="a1",
        body={},
        session_cookie="cookie1",
    )
    assert out["state"] == "reset"


@pytest.mark.asyncio
async def test_clear_error_route_calls_admin_clear_error():
    fake_admin = MagicMock()
    fake_admin.clear_agent_error = AsyncMock(return_value={"id": "a1", "status": "idle"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.clear_agent_error("a1", ctx=_ctx())
    fake_admin.clear_agent_error.assert_awaited_once_with(agent_id="a1", session_cookie="cookie1")
    assert out["status"] == "idle"


@pytest.mark.asyncio
async def test_recovery_routes_redact_adapter_fields():
    """Paperclip echoes the agent record on these ops, which can include
    adapterConfig/adapterType (the OpenClaw service token). The routes MUST
    redact them like the pause/resume handlers — never leak to the browser."""
    record = {
        "id": "a1",
        "status": "idle",
        "adapterConfig": {"authToken": "SECRET"},
        "adapterType": "openclaw_gateway",
    }
    fake_admin = MagicMock()
    fake_admin.reset_agent_session = AsyncMock(return_value=dict(record))
    fake_admin.clear_agent_error = AsyncMock(return_value=dict(record))
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        reset = await agents_mod.reset_agent_session("a1", body=ResetAgentSessionBody(), ctx=_ctx())
        cleared = await agents_mod.clear_agent_error("a1", ctx=_ctx())
    for out in (reset, cleared):
        assert "adapterConfig" not in out
        assert "adapterType" not in out
        assert out["id"] == "a1"


def test_reset_agent_session_body_forbids_extra_and_aliases():
    """``extra="forbid"`` rejects a smuggled adapter field; ``task_key``
    serializes to the camelCase ``taskKey`` Paperclip expects."""
    import pydantic

    body = ResetAgentSessionBody(task_key="t1")
    assert body.model_dump(by_alias=True, exclude_none=True) == {"taskKey": "t1"}
    with pytest.raises(pydantic.ValidationError):
        ResetAgentSessionBody(task_key="t1", adapterType="openclaw_gateway")
