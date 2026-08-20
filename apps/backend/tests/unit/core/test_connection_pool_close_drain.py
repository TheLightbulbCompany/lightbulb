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

    async def mark_gateway_connected(self, owner_id, *, conn_token=None):
        pass

    async def mark_gateway_disconnected(self, owner_id, *, conn_token=None):
        pass

    async def refresh_gateway_liveness(self, owner_id, *, conn_token=None):
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
    conn._run_completion_tasks = set()
    conn._analytics_tasks = set()
    # close() calls _reject_pending_run_completions, which reads this registry;
    # a __new__-built double must mirror every attribute the close path touches
    # (real GatewayConnection.__init__ sets both this and _run_completion_tasks).
    conn._run_completion_callbacks = {}
    conn._grace_task = None
    conn._parked_watchdog_task = None
    conn._reader_task = None
    conn._active_runs = {}
    conn._parked = False
    conn._emit_status_change = lambda *a, **k: None
    conn._liveness_refresh_task = None
    conn._liveness_lock = asyncio.Lock()
    conn._liveness_dead = False
    conn._liveness_token = "tok-test"
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

    async def mark_gateway_connected(self, owner_id, *, conn_token=None):
        self._calls.append("connected")

    async def mark_gateway_disconnected(self, owner_id, *, conn_token=None):
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
async def test_disconnect_serializes_delete_behind_inflight_refresh_then_cancels():
    """Round-7 resurrection race: cancelling the refresh loop mid-write releases
    _liveness_lock WITHOUT stopping the boto3 write already running, so the
    terminal delete could land first and the still-running refresh then recreate
    a dead marker via its absent-row recovery path. The fix: the delete must
    serialize behind the in-flight refresh (wait for the lock), and the refresh
    task is cancelled ONLY AFTER the delete lands -- catching it in sleep."""
    order = []

    class _OrderedSvc:
        async def mark_gateway_disconnected(self, owner_id, *, conn_token=None):
            order.append("delete")

    conn = _make_conn()
    conn._conn_svc = _OrderedSvc()

    # A stand-in refresh task we can observe. It parks (as the real loop does
    # between ticks); the disconnect must cancel it, but only after the delete.
    async def _parked_refresh():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("refresh-cancelled")
            raise

    conn._liveness_refresh_task = asyncio.create_task(_parked_refresh())
    await asyncio.sleep(0)

    # Simulate a refresh mid-write: it holds the lock, its server write not yet
    # done. The disconnect note must NOT synchronously cancel the refresh, and
    # its delete must block on the lock.
    await conn._liveness_lock.acquire()
    conn._note_gateway_liveness(connected=False)
    await asyncio.sleep(0.02)
    assert conn._liveness_dead is True
    assert "delete" not in order  # delete is queued behind the held lock
    assert not conn._liveness_refresh_task.done()  # not cancelled mid-write

    # In-flight refresh finishes and releases the lock -> delete lands, THEN the
    # (now-parked) refresh task is cancelled.
    conn._liveness_lock.release()
    await asyncio.sleep(0.02)
    assert order == ["delete", "refresh-cancelled"]


@pytest.mark.asyncio
async def test_liveness_write_nudges_status_on_flip(monkeypatch):
    """A liveness connect/disconnect moves is_gateway_connected, which
    compute_status composes -- so each LANDED write fires the realtime nudge.
    The dead-gated connect no-op writes nothing, so it must NOT nudge."""
    from core.services import status_events

    nudged = []
    monkeypatch.setattr(status_events, "publish_status_changed", lambda oid: nudged.append(oid))

    conn = _make_conn()
    conn._conn_svc = _RecordingConnSvc([])

    await conn._write_liveness(connected=True)  # landed put
    await conn._write_liveness(connected=False)  # landed delete
    assert nudged == ["u1", "u1"]

    # Dead-gated connect: returns before writing -> no nudge.
    conn._liveness_dead = True
    await conn._write_liveness(connected=True)
    assert nudged == ["u1", "u1"]


@pytest.mark.asyncio
async def test_unresolved_rpc_rejected_after_drain_timeout(monkeypatch):
    monkeypatch.setattr(cp, "_RPC_DRAIN_TIMEOUT", 0.1)
    conn = _make_conn()
    fut = asyncio.get_event_loop().create_future()
    conn._pending_rpcs["r1"] = fut
    await conn.close()
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
