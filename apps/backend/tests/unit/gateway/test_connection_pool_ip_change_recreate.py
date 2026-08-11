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

    old.close.assert_awaited_once()  # the stale (dead-IP) socket was torn down
    pool._create_connection.assert_awaited_once()  # and a new one was created
    assert got is fresh
    assert pool._connections["user_1"] is fresh


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
