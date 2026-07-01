"""Forwarded event frames carry the originating session_id for per-session demux."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.gateway.connection_pool import GatewayConnection  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture
def conn():
    c = GatewayConnection(
        frontend_connections=set(),
        user_id="user-1",
        ip="10.0.0.1",
        token="tok",
        management_api=MagicMock(),
        owner_id="user-1",
    )
    captured: list[dict] = []

    async def _stub_forward(message, target_member_id=None):
        captured.append(message)

    c._forward_to_frontends = _stub_forward  # type: ignore[assignment]
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
async def test_done_frame_carries_session_id_for_4part_key(conn):
    await _final(conn, f"agent:luna:user-1:{_UUID}")
    done = [f for f in conn._captured if f.get("type") == "done"]
    assert done and done[0]["session_id"] == _UUID


@pytest.mark.asyncio
async def test_legacy_key_omits_session_id(conn):
    await _final(conn, "agent:luna:user-1")
    done = [f for f in conn._captured if f.get("type") == "done"]
    assert done and "session_id" not in done[0]


@pytest.mark.asyncio
async def test_error_frame_carries_session_id(conn):
    await conn._handle_message(
        {
            "type": "event",
            "event": "chat",
            "payload": {
                "sessionKey": f"agent:luna:user-1:{_UUID}",
                "state": "error",
                "runId": "run_2",
                "errorMessage": "boom",
            },
        }
    )
    err = [f for f in conn._captured if f.get("type") == "error"]
    assert err and err[0]["session_id"] == _UUID
