"""Pending run-completion observers must be reconciled on teardown, not dropped.

bootstrap_dispatcher registers a run-completion callback to move onboarding
dispatched→delivered/failed and deliberately does NOT re-arm post-connect. If
the connection is torn down (IP-change recreate, dead reader loop, park→dead
escalation, or any close) before the terminal chat event arrives, that callback
must be fired as errored so onboarding fails-and-recovers instead of sticking at
`dispatched` forever.
"""

import asyncio

import pytest

from core.gateway.connection_pool import GatewayConnection


def _conn_with_callback(fired: list):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._run_completion_tasks = set()

    async def cb(state, payload):
        fired.append((state, payload))

    conn._run_completion_callbacks = {"run-1": cb}
    return conn


@pytest.mark.asyncio
async def test_reject_pending_run_completions_fires_and_empties():
    fired: list = []
    conn = _conn_with_callback(fired)

    conn._reject_pending_run_completions("boom")

    assert conn._run_completion_callbacks == {}  # popped so it can't fire twice
    await asyncio.gather(*list(conn._run_completion_tasks))
    assert fired == [("error", {"errorMessage": "boom"})]


@pytest.mark.asyncio
async def test_close_reconciles_pending_completion_callback():
    fired: list = []
    conn = _conn_with_callback(fired)
    # Minimal state for close() to run its drain path.
    conn._closed = False
    conn._billing_tasks = set()
    conn._push_tasks = set()
    conn._pending_rpcs = {}
    conn._grace_task = None
    conn._parked_watchdog_task = None
    conn._reader_task = None
    conn._ws = None
    conn._active_runs = {}
    conn._parked = False
    conn._emit_status_change = lambda state, reason: None
    conn._note_gateway_liveness = lambda *, connected: None

    await conn.close()

    # close() fired the observer AND its drain awaited the DDB write.
    assert fired == [("error", {"errorMessage": "Gateway connection closed"})]


@pytest.mark.asyncio
async def test_dead_reader_exit_reconciles_pending_completion_callback():
    fired: list = []
    conn = _conn_with_callback(fired)
    conn._active_runs = {}  # no active runs → DEAD path
    conn._pending_rpcs = {}
    conn._emit_status_change = lambda state, reason: None
    conn._note_gateway_liveness = lambda *, connected: None

    conn._on_reader_loop_exit(RuntimeError("reader died"))

    await asyncio.gather(*list(conn._run_completion_tasks))
    assert fired == [("error", {"errorMessage": "Gateway connection lost"})]
