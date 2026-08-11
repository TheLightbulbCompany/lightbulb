"""GW-4a: a CLEAN reader-loop exit must run the same teardown as an error exit.

``async for raw in self._ws`` ends WITHOUT raising when the peer closes the
socket normally (ConnectionClosedOK — e.g. a graceful OpenClaw restart). The
``except Exception`` branch never fired, so pending RPCs hung the full timeout,
the DDB liveness row stayed "connected", and no GATEWAY_DOWN reached the UI. The
normal-exit path is now routed through ``_on_reader_loop_exit`` too.
"""

import asyncio

import pytest

from core.gateway.connection_pool import GatewayConnection


class _CleanCloseWS:
    """Async-iterable that ends immediately with no error — models the
    ConnectionClosedOK the websockets async iterator swallows on a clean close."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_clean_close_rejects_pending_and_marks_liveness_down():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = "user_1"
    conn.user_id = "user_1"
    conn._ws = _CleanCloseWS()
    conn._closed = False
    conn._parked = False
    conn._active_runs = {}
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()

    pending = asyncio.get_running_loop().create_future()
    conn._pending_rpcs = {"r1": pending}

    status_changes: list[tuple[str, str]] = []
    liveness: list[bool] = []
    conn._emit_status_change = lambda state, reason: status_changes.append((state, reason))
    conn._note_gateway_liveness = lambda *, connected: liveness.append(connected)

    await conn._reader_loop()

    # Pending RPC was rejected (not left to hang the full timeout).
    assert pending.done()
    with pytest.raises(RuntimeError, match="Gateway connection lost"):
        pending.result()
    assert conn._pending_rpcs == {}

    # Durable liveness marked down + UI told the gateway is gone.
    assert liveness == [False]
    assert status_changes and status_changes[-1][0] == "GATEWAY_DOWN_DEAD"


@pytest.mark.asyncio
async def test_clean_close_after_explicit_close_is_a_noop():
    # If the connection was already closed deliberately, the clean-exit path must
    # NOT double-fire teardown.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = "user_1"
    conn.user_id = "user_1"
    conn._ws = _CleanCloseWS()
    conn._closed = True
    conn._parked = False
    conn._active_runs = {}
    conn._pending_rpcs = {}

    fired: list[str] = []
    conn._emit_status_change = lambda state, reason: fired.append(state)
    conn._note_gateway_liveness = lambda *, connected: fired.append("liveness")

    await conn._reader_loop()

    assert fired == []
