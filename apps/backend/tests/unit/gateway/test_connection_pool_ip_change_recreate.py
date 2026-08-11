"""GW-1: a CHANGED task IP must recreate the connection, not just adopt the field.

An ECS task replacement hands out a new private IP but leaves the old socket
reporting is_connected==True (the local websocket-state flag reads OPEN for up
to the ping timeout because the old ENI vanished with no FIN). The prior code
merely did ``conn.ip = ip`` and then, because the socket still looked OPEN,
handed the stale connection straight back — so every RPC on it timed out
against a dead address (a real prod outage). ``ensure_connection`` must tear the
connection down and recreate it against the freshly-resolved endpoint.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.gateway.connection_pool import GatewayConnectionPool


class _FakeConn:
    def __init__(self, ip: str, *, connected: bool = True, parked: bool = False):
        self.ip = ip
        self.is_connected = connected
        self._parked = parked
        self.close = AsyncMock()


def _bare_pool() -> GatewayConnectionPool:
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._closing = False
    pool._lock = asyncio.Lock()
    pool._connections = {}
    pool._frontend_connections = {}
    pool._last_reconnect_at = {}
    pool._auth_backoff_until = {}
    pool._auth_failures = {}
    pool._background_close_tasks = set()
    return pool


@pytest.mark.asyncio
async def test_changed_ip_tears_down_and_recreates():
    pool = _bare_pool()
    old = _FakeConn("10.0.0.1", connected=True, parked=False)
    pool._connections["user_1"] = old

    fresh = _FakeConn("10.0.0.2")

    async def fake_create(user_id, ip, token):
        assert ip == "10.0.0.2"  # recreate uses the fresh endpoint
        pool._connections[user_id] = fresh
        return fresh

    pool._create_connection = AsyncMock(side_effect=fake_create)

    got = await pool.ensure_connection("user_1", "10.0.0.2", "tok")

    pool._create_connection.assert_awaited_once()  # a new one was created
    assert got is fresh
    assert pool._connections["user_1"] is fresh
    # The stale (dead-IP) socket is torn down in the BACKGROUND (off the lock).
    await asyncio.gather(*pool._background_close_tasks)
    old.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_ip_does_not_hold_lock_across_close():
    """P1-2: close() drains for up to ~25s; it must run OFF the pool lock so it
    can't head-of-line-block ensure_connection for every other owner."""
    pool = _bare_pool()
    hang = asyncio.Event()
    old = _FakeConn("10.0.0.1")

    async def slow_close():
        await hang.wait()

    old.close = AsyncMock(side_effect=slow_close)
    pool._connections["user_1"] = old

    fresh = _FakeConn("10.0.0.2")

    async def fake_create(user_id, ip, token):
        pool._connections[user_id] = fresh
        return fresh

    pool._create_connection = AsyncMock(side_effect=fake_create)

    # Returns promptly even though the old connection's close is still draining.
    got = await asyncio.wait_for(pool.ensure_connection("user_1", "10.0.0.2", "tok"), timeout=1)
    assert got is fresh
    assert not pool._lock.locked()  # lock released despite the drain in flight
    assert pool._background_close_tasks  # close is running in the background

    hang.set()
    await asyncio.gather(*pool._background_close_tasks)


@pytest.mark.asyncio
async def test_unchanged_ip_reuses_live_connection():
    pool = _bare_pool()
    live = _FakeConn("10.0.0.1", connected=True, parked=False)
    pool._connections["user_1"] = live
    pool._create_connection = AsyncMock()

    got = await pool.ensure_connection("user_1", "10.0.0.1", "tok")

    assert got is live  # same connection, fast path
    live.close.assert_not_awaited()
    pool._create_connection.assert_not_awaited()
