"""Pending run-completion observers on teardown: fail them only when the run
is actually gone.

bootstrap_dispatcher registers a run-completion callback to move onboarding
sent→delivered/failed. Two teardowns look alike from inside the connection but
mean opposite things for the run:

* the GATEWAY dropped us (dead reader loop, park→dead escalation) — the run
  may well be dead; fire the observer as errored so onboarding fails-and-
  recovers instead of sticking at `sent` forever;
* THIS PROCESS let go of the socket (`close()`: graceful shutdown, grace close,
  IP-change recreate) — the kickoff keeps running inside OpenClaw. Marking it
  failed here is a lie the owner sees as "Friday couldn't start" while Friday
  is visibly working. The observer is handed off instead: the row stays `sent`
  and the dispatcher re-attaches an observer from whichever process polls next.
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
async def test_close_hands_off_pending_completion_callbacks_instead_of_failing_them():
    """close() is this process letting go of the socket, not evidence the run
    died — the observer must NOT fire as errored. It is left for the
    dispatcher to re-attach from the row (`bootstrap_state` stays `sent`)."""
    fired: list = []
    conn = _conn_with_callback(fired)
    # Minimal state for close() to run its drain path.
    conn._closed = False
    conn._billing_tasks = set()
    conn._analytics_tasks = set()
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

    assert fired == []


@pytest.mark.asyncio
async def test_terminal_arriving_during_close_drains_still_resolves_the_observer():
    """close() keeps observers registered through its drains (the reader is
    alive until it is cancelled near the end), so a genuine terminal that
    lands mid-close fires the observer instead of being dropped — the
    handoff only takes what is STILL unresolved once the reader is dead.
    Simulated: a billing task whose completion is a terminal arriving."""
    fired: list = []
    conn = _conn_with_callback(fired)
    conn._closed = False
    conn._analytics_tasks = set()
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

    async def _terminal_lands_mid_close():
        await asyncio.sleep(0)
        conn._dispatch_run_completion("run-1", "final", {"runId": "run-1"})

    conn._billing_tasks = {asyncio.create_task(_terminal_lands_mid_close())}

    await conn.close()

    assert fired == [("final", {"runId": "run-1"})]
    assert conn._run_completion_callbacks == {}


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
