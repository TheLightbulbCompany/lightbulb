"""Unit tests for `GET /messaging/runs/{run_id}` -- the reconnect reconciliation
read.

Run frames are ephemeral over the live channel, unlike `room.message.created`
-- they have no `afterSeq` gap recovery, so a client that reconnects or rotates
its delivery key mid-run can miss the terminal `run_ended` frame and be left
showing "Working" forever. This endpoint reads the run LEDGER (the
`OWNER#<owner>/RUN#<runId>` row), the same truth abort and steer resolve
against, with no side effect.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import RunStatusResponse

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"


def _auth():
    auth = MagicMock()
    auth.org_id = "org_1"
    auth.user_id = "user_1"
    return auth


@contextlib.contextmanager
def _ledger(row):
    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=row),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value={"id": AGENT_ID}),
    ):
        yield


@pytest.mark.asyncio
async def test_get_run_status_reports_running_for_an_active_run():
    from routers.messaging import get_run_status

    with _ledger({"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"}):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="running")


@pytest.mark.asyncio
async def test_get_run_status_reports_terminal_for_a_completed_run():
    from routers.messaging import get_run_status

    with _ledger({"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "ended"}):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="terminal")


@pytest.mark.asyncio
async def test_get_run_status_reports_terminal_for_a_run_it_has_no_memory_of():
    """A missing ledger row (swept, or never written) must read as terminal, not
    404 -- from the client's point of view "unknown" and "ended" call for the
    same action: stop showing it as running."""
    from routers.messaging import get_run_status

    with _ledger(None):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="terminal")


@pytest.mark.asyncio
async def test_get_run_status_reports_terminal_when_the_agent_left_the_room():
    """`_resolved_run` refuses a run whose agent is no longer a member
    (RUN_NOT_ACTIVE). The client still just needs to stop showing "Working"."""
    from routers.messaging import get_run_status

    with (
        patch(
            "routers.messaging.messaging_repo.get_run",
            new_callable=AsyncMock,
            return_value={"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"},
        ),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value=None),
        patch("routers.messaging.messaging_repo.get_room", AsyncMock(return_value={"ownerId": "o"})),  # not the feed
    ):
        result = await get_run_status("run-1", auth=_auth())

    assert result == RunStatusResponse(run_id="run-1", state="terminal")
