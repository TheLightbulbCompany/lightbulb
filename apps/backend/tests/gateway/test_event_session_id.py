"""Room-run terminal frames retain their native run identifier."""

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.gateway.connection_pool import GatewayConnection  # noqa: E402

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


@pytest.fixture
def conn():
    c = GatewayConnection(
        user_id="user-1",
        ip="10.0.0.1",
        token="tok",
        owner_id="user-1",
    )
    captured: list[dict] = []

    async def _stub_forward(_session_key, frame):
        captured.append(frame)

    c._publish_room_frame = _stub_forward  # type: ignore[assignment]
    c._captured = captured  # type: ignore[attr-defined]
    return c


async def _final(conn, session_key):
    await conn._handle_message(
        {
            "type": "event",
            "event": "chat",
            "payload": {"sessionKey": session_key, "state": "final", "runId": "run_1"},
        }
    )


@pytest.mark.asyncio
async def test_done_frame_carries_run_id_for_room_session(conn):
    await _final(conn, SESSION_KEY)
    done = [f for f in conn._captured if f.get("type") == "done"]
    assert done and done[0]["runId"] == "run_1"
    assert done[0]["agent_id"] == AGENT_ID


@pytest.mark.asyncio
async def test_non_room_key_does_not_publish_a_room_frame(conn):
    with patch("core.repositories.messaging_repo.get_room", new=AsyncMock()) as get_room:
        await GatewayConnection._publish_room_frame(
            conn,
            "agent:luna:user-1",
            {"type": "done", "runId": "run_1"},
        )
    get_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_frame_carries_run_id_for_room_session(conn):
    await conn._handle_message(
        {
            "type": "event",
            "event": "chat",
            "payload": {
                "sessionKey": SESSION_KEY,
                "state": "error",
                "runId": "run_2",
                "errorMessage": "boom",
            },
        }
    )
    err = [f for f in conn._captured if f.get("type") == "error"]
    assert err and err[0]["runId"] == "run_2"
    assert err[0]["agent_id"] == AGENT_ID
