import asyncio

import pytest

from core.gateway import connection_pool as cp


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _NoopConnSvc:
    """Stand-in for ConnectionService: close() fires the (best-effort) DDB
    gateway-liveness clear via _note_gateway_liveness; these __new__-built
    partial connections don't wire a real service, so no-op it."""

    async def mark_gateway_connected(self, owner_id):
        pass

    async def mark_gateway_disconnected(self, owner_id):
        pass

    async def refresh_gateway_liveness(self, owner_id):
        pass


def _make_conn():
    conn = cp.GatewayConnection.__new__(cp.GatewayConnection)
    conn.user_id = "u1"
    conn.owner_id = "u1"
    conn._closed = False
    conn._ws = _FakeWS()
    conn._pending_rpcs = {}
    conn._billing_tasks = set()
    conn._push_tasks = set()
    conn._grace_task = None
    conn._parked_watchdog_task = None
    conn._reader_task = None
    conn._active_runs = {}
    conn._parked = False
    conn._emit_status_change = lambda *a, **k: None
    conn._liveness_refresh_task = None
    conn._liveness_lock = asyncio.Lock()
    conn._liveness_dead = False
    conn._conn_svc = _NoopConnSvc()
    return conn


@pytest.mark.asyncio
async def test_inflight_rpc_resolved_during_drain_is_not_rejected():
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut

    # Resolve the future shortly after close() starts draining.
    async def _resolve():
        await asyncio.sleep(0.05)
        if not fut.done():
            fut.set_result({"ok": True})

    asyncio.create_task(_resolve())
    await conn.close()
    assert fut.done() and fut.exception() is None
    assert conn._ws.closed is True


class _RecordingConnSvc:
    def __init__(self, calls):
        self._calls = calls

    async def mark_gateway_connected(self, owner_id):
        self._calls.append("connected")

    async def mark_gateway_disconnected(self, owner_id):
        self._calls.append("disconnected")


@pytest.mark.asyncio
async def test_liveness_write_put_noops_after_disconnect():
    """A put (initial connect OR a refresh tick) that loses the race to a
    disconnect must NOT recreate the gateway-liveness row -- the
    _liveness_dead terminal flag gates it inside the serialization lock so a
    dead gateway can't read `ready` for the freshness window."""
    conn = _make_conn()
    calls = []
    conn._conn_svc = _RecordingConnSvc(calls)

    conn._liveness_dead = True  # a disconnect has been noted
    await conn._write_liveness(connected=True)
    assert calls == []  # put suppressed -- no resurrection

    # A delete always applies (terminal transition), regardless of the flag.
    await conn._write_liveness(connected=False)
    assert calls == ["disconnected"]

    # After a genuine reconnect clears the flag, puts apply again.
    conn._liveness_dead = False
    await conn._write_liveness(connected=True)
    assert calls == ["disconnected", "connected"]


@pytest.mark.asyncio
async def test_unresolved_rpc_rejected_after_drain_timeout(monkeypatch):
    monkeypatch.setattr(cp, "_RPC_DRAIN_TIMEOUT", 0.1)
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut
    await conn.close()
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
