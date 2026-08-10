"""The grace-close teardown must not fire while a run is active.

``has_active_runs`` is a ``@property`` on ``GatewayConnection`` (not a method),
so the guard reads it as an attribute (no parens). These tests mirror that by
setting/mutating ``conn.has_active_runs`` as a plain attribute on the mock.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import (
    GatewayConnectionPool,
    _ACTIVE_RUN_POLL_S,
    _PUSH_GRACE_CEILING_S,
)


def _pool_with_active_run(active: bool):
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {}  # no clients connected
    pool._grace_tasks = {}
    conn = MagicMock()
    conn.has_active_runs = active  # property form: attribute, not callable
    pool._connections = {"user_1": conn}

    # Honor close_user's real contract: the connection entry is popped
    # SYNCHRONOUSLY, before any await. _grace_close's exit re-arm keys on
    # that pop — a mock that leaves the entry would make the re-arm chain a
    # second (spurious) cycle and fail these tests for a reason production
    # cannot exhibit.
    async def _close(user_id):
        pool._connections.pop(user_id, None)
        pool._grace_tasks.pop(user_id, None)

    pool.close_user = AsyncMock(side_effect=_close)
    return pool


@pytest.mark.asyncio
async def test_closes_when_no_active_run():
    pool = _pool_with_active_run(active=False)
    # Patch sleeps so the test doesn't wait real seconds.
    with patch("core.gateway.connection_pool.asyncio.sleep", AsyncMock()):
        await pool._grace_close("user_1")
    pool.close_user.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
async def test_defers_while_run_active_then_closes():
    pool = _pool_with_active_run(active=True)
    conn = pool._connections["user_1"]

    poll_sleeps = {"n": 0}

    async def fake_sleep(seconds):
        # The run clears after the first active-run poll so the loop exits.
        if seconds == _ACTIVE_RUN_POLL_S:
            poll_sleeps["n"] += 1
            conn.has_active_runs = False

    with patch("core.gateway.connection_pool.asyncio.sleep", side_effect=fake_sleep):
        await pool._grace_close("user_1")

    # It DID hold (polled at least once) before closing.
    assert poll_sleeps["n"] >= 1
    # Still closes once the run clears and no clients returned.
    pool.close_user.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
async def test_hold_is_bounded_by_ceiling():
    # Run never clears — the guard must still terminate at the ceiling.
    pool = _pool_with_active_run(active=True)

    poll_sleeps = {"n": 0}

    async def fake_sleep(seconds):
        if seconds == _ACTIVE_RUN_POLL_S:
            poll_sleeps["n"] += 1

    with patch("core.gateway.connection_pool.asyncio.sleep", side_effect=fake_sleep):
        await pool._grace_close("user_1")

    # Bounded: exactly ceiling / poll cadence polls, then close.
    assert poll_sleeps["n"] == _PUSH_GRACE_CEILING_S // _ACTIVE_RUN_POLL_S
    pool.close_user.assert_awaited_once_with("user_1")
