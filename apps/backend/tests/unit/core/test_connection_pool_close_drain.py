import asyncio

import pytest

from core.gateway import connection_pool as cp


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _make_conn():
    conn = cp.GatewayConnection.__new__(cp.GatewayConnection)
    conn.user_id = "u1"
    conn._closed = False
    conn._ws = _FakeWS()
    conn._pending_rpcs = {}
    conn._billing_tasks = set()
    conn._grace_task = None
    conn._parked_watchdog_task = None
    conn._reader_task = None
    conn._active_runs = {}
    conn._parked = False
    conn._emit_status_change = lambda *a, **k: None
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


@pytest.mark.asyncio
async def test_unresolved_rpc_rejected_after_drain_timeout(monkeypatch):
    monkeypatch.setattr(cp, "_RPC_DRAIN_TIMEOUT", 0.1)
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut
    await conn.close()
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
