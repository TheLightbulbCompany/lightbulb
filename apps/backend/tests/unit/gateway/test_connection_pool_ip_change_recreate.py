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
import time
from unittest.mock import AsyncMock

import pytest

from core.gateway.connection_pool import GatewayConnectionPool, GatewayReconnectThrottled


class _FakeConn:
    def __init__(self, ip: str, *, connected: bool = True, parked: bool = False):
        self.ip = ip
        self.user_id = "user_1"
        self.is_connected = connected
        self._parked = parked
        self.close = AsyncMock()
        self.drain_liveness_write = AsyncMock()


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
    pool._superseded_ip = {}
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
    # The abandoned endpoint is recorded in the fence history (stamped at detach)
    # so a later stale resolve can't roll it back.
    assert "10.0.0.1" in pool._superseded_ip["user_1"]
    # The stale (dead-IP) socket is torn down in the BACKGROUND (off the lock).
    await asyncio.gather(*pool._background_close_tasks)
    old.close.assert_awaited_once()
    # The terminal liveness delete is DRAINED inside the background task (close()
    # only schedules it), so a task finishing before shutdown can't orphan the
    # write and leave the detached dead connection's row falsely "ready".
    old.drain_liveness_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_detached_close_drains_liveness_even_when_close_raises():
    """close() schedules the terminal liveness delete UP FRONT, then can raise in
    its ws teardown. The background task must still drain that write (from a
    finally) or shutdown orphans it and the dead detached row reads 'ready'."""
    pool = _bare_pool()
    old = _FakeConn("10.0.0.1")
    old.close = AsyncMock(side_effect=RuntimeError("ws close blew up"))
    pool._connections["user_1"] = old

    fresh = _FakeConn("10.0.0.2")

    async def fake_create(user_id, ip, token):
        pool._connections[user_id] = fresh
        return fresh

    pool._create_connection = AsyncMock(side_effect=fake_create)

    got = await pool.ensure_connection("user_1", "10.0.0.2", "tok")
    assert got is fresh
    await asyncio.gather(*pool._background_close_tasks)
    old.close.assert_awaited_once()
    # Drained despite close() raising — the write is landed, not orphaned.
    old.drain_liveness_write.assert_awaited_once()


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
async def test_stale_endpoint_does_not_roll_back_newer_connection():
    """A STALE resolve result (a concurrent caller straddling a task replacement,
    made worse because the dead task's health probe can burn a full 5s) must NOT
    tear down the newer HEALTHY connection and dial the dead task — that would
    recreate the outage the recreate exists to close."""
    pool = _bare_pool()
    # The NEW connection is already installed at 10.0.0.2...
    live = _FakeConn("10.0.0.2", connected=True, parked=False)
    pool._connections["user_1"] = live
    # ...and 10.0.0.1 was the endpoint we just recreated AWAY from (superseded).
    pool._superseded_ip["user_1"] = {"10.0.0.1": time.monotonic()}
    pool._create_connection = AsyncMock()

    # A late request resolves the STALE old IP.
    got = await pool.ensure_connection("user_1", "10.0.0.1", "tok")

    assert got is live  # pinned to the live endpoint, not rolled back
    pool._create_connection.assert_not_awaited()  # no recreate onto the dead task
    live.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_superseded_history_survives_a_second_replacement():
    """P1: across two fast replacements (A→B→C) a resolution still carrying the
    FIRST abandoned IP (A) must stay fenced even though B has since abandoned its
    own slot. A single-slot fence would have dropped A when B was recorded, so the
    A-carrying caller would detach the healthy C, dial dead A, and — worse —
    record C as superseded, wedging every later resolve of the real live task."""
    pool = _bare_pool()
    live = _FakeConn("10.0.0.3", connected=True, parked=False)  # C is current + healthy
    pool._connections["user_1"] = live
    # Both earlier endpoints are in the bounded history.
    pool._superseded_ip["user_1"] = {
        "10.0.0.1": time.monotonic(),  # A
        "10.0.0.2": time.monotonic(),  # B
    }
    pool._create_connection = AsyncMock()

    got = await pool.ensure_connection("user_1", "10.0.0.1", "tok")  # stale A

    assert got is live  # pinned to C, not rolled back to A
    pool._create_connection.assert_not_awaited()
    live.close.assert_not_awaited()  # healthy C never detached
    assert "10.0.0.3" not in pool._superseded_ip["user_1"]  # C never wrongly fenced


@pytest.mark.asyncio
async def test_superseded_endpoint_refused_on_absent_owner_not_dialed():
    """Finding 1: when a recreate detaches the old connection and the replacement
    dial then RAISES, the owner is left ABSENT but the stale-endpoint fence — now
    stamped at detach — still names the dead IP. A concurrent caller bearing that
    IP reaches the absent path (no live conn to pin to); it must be REFUSED, not
    dialed straight onto the dead task."""
    pool = _bare_pool()
    # Owner absent (replacement dial failed), fence recorded at the earlier detach.
    pool._superseded_ip["user_1"] = {"10.0.0.1": time.monotonic()}
    pool._last_reconnect_at["user_1"] = time.monotonic() - 999  # floor not the cause
    pool._create_connection = AsyncMock()

    with pytest.raises(GatewayReconnectThrottled):
        await pool.ensure_connection("user_1", "10.0.0.1", "tok")
    pool._create_connection.assert_not_awaited()  # never dialed the dead endpoint


@pytest.mark.asyncio
async def test_reconnect_floor_applies_during_changed_ip_repair():
    """After a detach the owner is ABSENT from _connections, so a queued caller
    reaches the create path with no preceding branch; the floor must still apply
    (for an owner that has reconnected before) so fast connect failures can't
    storm the container."""
    pool = _bare_pool()
    # Owner absent (detached), but reconnected recently — within the floor.
    pool._last_reconnect_at["user_1"] = time.monotonic()
    pool._create_connection = AsyncMock()

    with pytest.raises(GatewayReconnectThrottled):
        await pool.ensure_connection("user_1", "10.0.0.9", "tok")
    pool._create_connection.assert_not_awaited()  # throttled before dialing


@pytest.mark.asyncio
async def test_first_ever_connection_not_throttled():
    """A genuine first connection (no prior reconnect stamp) is never made to
    wait on the floor."""
    pool = _bare_pool()
    fresh = _FakeConn("10.0.0.1")

    async def fake_create(user_id, ip, token):
        pool._connections[user_id] = fresh
        return fresh

    pool._create_connection = AsyncMock(side_effect=fake_create)

    got = await pool.ensure_connection("user_1", "10.0.0.1", "tok")
    assert got is fresh
    pool._create_connection.assert_awaited_once()


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
