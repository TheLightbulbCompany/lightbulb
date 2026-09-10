"""Unit tests for `GET /messaging/runs/{run_id}` -- the reconnect reconciliation
read added alongside the room-run "stuck on Working" fix.

`room.run.frame` (which carries the terminal `run_ended`) is ephemeral over the
live channel, unlike `room.message.created` -- it has no `afterSeq` gap
recovery. This endpoint reuses the exact same `pool.resolve_run` truth as
abort/steer (`_resolved_active_run`) so the frontend can self-heal on
reconnect instead of leaving a run stuck showing "running" forever.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import RunStatusResponse

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


def _auth():
    auth = MagicMock()
    auth.org_id = "org_1"
    auth.user_id = "user_1"
    return auth


@pytest.mark.asyncio
async def test_get_run_status_reports_running_for_an_active_run():
    from routers.messaging import get_run_status

    fake_pool = MagicMock()
    fake_pool.resolve_run.return_value = {"sessionKey": SESSION_KEY, "agentId": AGENT_ID, "terminal": False}

    with (
        patch("routers.messaging.get_gateway_pool", return_value=fake_pool),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value={"id": AGENT_ID}),
    ):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="running")


@pytest.mark.asyncio
async def test_get_run_status_reports_terminal_for_a_completed_run():
    from routers.messaging import get_run_status

    fake_pool = MagicMock()
    fake_pool.resolve_run.return_value = {"sessionKey": SESSION_KEY, "terminal": True}

    with (
        patch("routers.messaging.get_gateway_pool", return_value=fake_pool),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value={"id": AGENT_ID}),
    ):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="terminal")


@pytest.mark.asyncio
async def test_get_run_status_reports_terminal_for_a_run_it_has_no_memory_of():
    """`resolve_run` returning None (evicted terminal-id cache, reconnected
    container link, ...) must read as terminal, not 404 -- from the client's
    point of view "unknown" and "ended" call for the same action: stop
    showing it as running."""
    from routers.messaging import get_run_status

    fake_pool = MagicMock()
    fake_pool.resolve_run.return_value = None

    with patch("routers.messaging.get_gateway_pool", return_value=fake_pool):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="terminal")
