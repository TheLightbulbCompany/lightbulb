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
    conn._terminal_run_ids = {}
    conn._analytics_tasks = set()
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._run_text = {}
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
        patch.object(conn, "_publish_room_frame", AsyncMock()),
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


# --- unregister_run_completion_callback (a dispatcher's own send failed AFTER
# registering, so no terminal will ever arrive to consume it -- see
# bootstrap_dispatcher.maybe_dispatch_bootstrap's except block) -------------


def test_unregister_withdraws_a_registered_callback():
    conn = _make_terminal_conn()

    async def cb(state, payload):
        pass

    conn.register_run_completion_callback("run-1", cb)
    assert "run-1" in conn._run_completion_callbacks

    conn.unregister_run_completion_callback("run-1")
    assert "run-1" not in conn._run_completion_callbacks


def test_unregister_is_idempotent_on_an_unregistered_run_id():
    conn = _make_terminal_conn()
    # Must not raise -- nothing was ever registered for this run_id.
    conn.unregister_run_completion_callback("never-registered")


@pytest.mark.asyncio
async def test_unregistered_callback_does_not_fire_on_a_later_terminal():
    """The whole point: after unregister, a terminal event for that run_id
    must NOT invoke the (withdrawn) callback."""
    conn = _make_terminal_conn()
    seen: list[str] = []

    async def cb(state, payload):
        seen.append(state)

    conn.register_run_completion_callback("run-1", cb)
    conn.unregister_run_completion_callback("run-1")
    await _drive_terminal(conn, state="final", run_id="run-1")

    assert seen == []


def test_pool_unregister_run_completion_callback_no_connection_is_a_noop():
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._connections = {}
    # Must not raise.
    pool.unregister_run_completion_callback("owner_1", "run-1")


def test_pool_unregister_run_completion_callback_delegates_to_connection():
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    conn = _make_terminal_conn(owner_id="owner_1")
    pool._connections = {"owner_1": conn}

    async def cb(state, payload):
        pass

    pool.register_run_completion_callback("owner_1", "run-1", cb)
    pool.unregister_run_completion_callback("owner_1", "run-1")
    assert "run-1" not in conn._run_completion_callbacks


# --- has_run_completion_callback: "does THIS process already observe that run?" ---


@pytest.mark.asyncio
async def test_has_run_completion_callback_tracks_registration_and_consumption():
    conn = _make_terminal_conn()

    async def cb(state, payload):
        return None

    assert conn.has_run_completion_callback("run-1") is False
    conn.register_run_completion_callback("run-1", cb)
    assert conn.has_run_completion_callback("run-1") is True
    await _drive_terminal(conn, state="final", run_id="run-1")
    # Consumed by the terminal -> no longer observed here.
    assert conn.has_run_completion_callback("run-1") is False


def test_pool_has_run_completion_callback_is_false_without_a_connection():
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._connections = {}
    assert pool.has_run_completion_callback("org_1", "run-1") is False

    conn = _make_terminal_conn("org_1")
    pool._connections["org_1"] = conn
    assert pool.has_run_completion_callback("org_1", "run-1") is False
    conn.register_run_completion_callback("run-1", AsyncMock())
    assert pool.has_run_completion_callback("org_1", "run-1") is True
