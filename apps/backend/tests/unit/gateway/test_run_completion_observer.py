"""Unit tests for the run-completion observer mechanism (INV-5).

``register_run_completion_callback`` lets a caller that is NOT a browser tab
(the server-owned bootstrap dispatcher) observe a SPECIFIC dispatched run's
TRUE terminal (final/error/aborted) without depending on frame forwarding.
Driven through the real ``_handle_message`` seam, mirroring
``test_push_dispatch.py``'s ``_make_terminal_conn`` fixture.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


def _make_terminal_conn(owner_id="org_1"):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = owner_id
    conn.owner_id = owner_id
    conn._frontend_connections = set()
    conn._pending_rpcs = {}
    conn._management_api = MagicMock()
    conn._billing_tasks = set()
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    conn._conn_svc = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    conn._recipients_cache = None
    conn._recipients_cache_ts = 0.0
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._parked = False
    conn._parked_watchdog_task = None
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()
    return conn


async def _drive_terminal(conn, *, state: str, run_id: str = "run-1", session_key: str = "agent:a1:main"):
    payload = {
        "sessionKey": session_key,
        "state": state,
        "runId": run_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }
    if state == "error":
        payload["errorMessage"] = "boom"
    with (
        patch("core.services.push_service.send_push_to_user", AsyncMock()),
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
        patch.object(conn, "_resolve_agent_name", AsyncMock(return_value="Friday")),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks), *list(conn._run_completion_tasks))


@pytest.mark.asyncio
async def test_registered_callback_fires_on_final():
    conn = _make_terminal_conn()
    seen: list[tuple[str, dict]] = []

    async def cb(state, payload):
        seen.append((state, payload))

    conn.register_run_completion_callback("run-1", cb)
    await _drive_terminal(conn, state="final", run_id="run-1")

    assert len(seen) == 1
    assert seen[0][0] == "final"
    assert seen[0][1]["runId"] == "run-1"
    # Consumed exactly once -- popped off the registry.
    assert "run-1" not in conn._run_completion_callbacks


@pytest.mark.asyncio
async def test_registered_callback_fires_on_error():
    conn = _make_terminal_conn()
    seen: list[str] = []

    async def cb(state, payload):
        seen.append(state)

    conn.register_run_completion_callback("run-1", cb)
    await _drive_terminal(conn, state="error", run_id="run-1")

    assert seen == ["error"]


@pytest.mark.asyncio
async def test_registered_callback_fires_on_aborted():
    conn = _make_terminal_conn()
    seen: list[str] = []

    async def cb(state, payload):
        seen.append(state)

    conn.register_run_completion_callback("run-1", cb)
    await _drive_terminal(conn, state="aborted", run_id="run-1")

    assert seen == ["aborted"]


@pytest.mark.asyncio
async def test_unregistered_run_id_is_a_noop():
    """No callback registered for this run_id -- terminal handling proceeds
    normally (no crash, no spurious task)."""
    conn = _make_terminal_conn()
    await _drive_terminal(conn, state="final", run_id="some-other-run")
    assert conn._run_completion_tasks == set() or all(t.done() for t in conn._run_completion_tasks)


@pytest.mark.asyncio
async def test_callback_exception_is_swallowed():
    """A raising observer must never break terminal frame delivery -- fire
    and forget, same posture as _dispatch_push_notification."""
    conn = _make_terminal_conn()

    async def bad_cb(state, payload):
        raise RuntimeError("observer exploded")

    conn.register_run_completion_callback("run-1", bad_cb)
    # Must not raise.
    await _drive_terminal(conn, state="final", run_id="run-1")


@pytest.mark.asyncio
async def test_different_run_id_callback_not_consumed():
    """Registering for run-A must not fire (or clear) on run-B's terminal."""
    conn = _make_terminal_conn()
    seen: list[str] = []

    async def cb(state, payload):
        seen.append(state)

    conn.register_run_completion_callback("run-A", cb)
    await _drive_terminal(conn, state="final", run_id="run-B")

    assert seen == []
    assert "run-A" in conn._run_completion_callbacks


def test_pool_register_run_completion_callback_no_connection_returns_false():
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._connections = {}

    async def cb(state, payload):
        pass

    assert pool.register_run_completion_callback("owner_1", "run-1", cb) is False


def test_pool_register_run_completion_callback_delegates_to_connection():
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    conn = _make_terminal_conn(owner_id="owner_1")
    pool._connections = {"owner_1": conn}

    async def cb(state, payload):
        pass

    assert pool.register_run_completion_callback("owner_1", "run-1", cb) is True
    assert conn._run_completion_callbacks["run-1"] is cb
