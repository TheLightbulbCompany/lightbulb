"""arm_idle_teardown_if_unattended: the self-heal dial's teardown hook.

``remove_frontend_connection`` is otherwise the only armer of ``_grace_close``
— it fires on a frontend LEAVING. A gateway connection created while the
frontend set is already empty (the status poll's self-heal dial) never
traverses that path, so without this hook nothing would ever close it and a
trial container would stay running with nobody attached.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnectionPool

USER = "user_1"


def _pool(*, frontends=None, has_conn=True, grace_task=None):
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {USER: frontends} if frontends else {}
    pool._connections = {USER: MagicMock()} if has_conn else {}
    pool._grace_tasks = {USER: grace_task} if grace_task is not None else {}
    # _grace_close would hit real sleeps/repos; the armed task must simply
    # exist. Replace it with a stub coroutine the created task runs.
    pool._grace_close = lambda user_id: asyncio.sleep(0)
    return pool


@pytest.mark.asyncio
async def test_arms_when_unattended():
    pool = _pool()
    pool.arm_idle_teardown_if_unattended(USER)
    task = pool._grace_tasks.get(USER)
    assert task is not None
    await task


@pytest.mark.asyncio
async def test_noop_when_frontend_registered():
    pool = _pool(frontends={"conn-1"})
    pool.arm_idle_teardown_if_unattended(USER)
    assert USER not in pool._grace_tasks


@pytest.mark.asyncio
async def test_noop_when_no_gateway_connection():
    pool = _pool(has_conn=False)
    pool.arm_idle_teardown_if_unattended(USER)
    assert USER not in pool._grace_tasks


@pytest.mark.asyncio
async def test_pending_grace_task_is_left_alone():
    """A pending grace task IS coverage (its exit re-arm guarantees a
    replacement connection gets a fresh cycle) — the arm must never cancel
    it: a cancel landing mid-close_user abandons the old connection's
    drain, and shielding the drain lets its completion erase a returning
    client's registration."""

    async def _pending():
        await asyncio.sleep(3600)

    pending = asyncio.create_task(_pending())
    pool = _pool(grace_task=pending)
    pool.arm_idle_teardown_if_unattended(USER)
    assert pool._grace_tasks[USER] is pending
    assert not pending.cancelled()
    pending.cancel()


@pytest.mark.asyncio
async def test_rearms_over_a_finished_grace_task():
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    pool = _pool(grace_task=done)
    pool.arm_idle_teardown_if_unattended(USER)
    task = pool._grace_tasks[USER]
    assert task is not done
    await task


@pytest.mark.asyncio
async def test_grace_exit_rearms_a_replacement_connection(monkeypatch):
    """The exit guarantee behind skip-if-pending: a replacement connection
    dialed while the grace task was mid-cycle gets its OWN fresh cycle when
    that task exits, and the chain terminates once no connection remains."""
    from core.gateway import connection_pool as cp
    from core.repositories import billing_repo

    monkeypatch.setattr(cp, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=None))

    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {}
    old_conn = MagicMock()
    old_conn.has_active_runs = False
    pool._connections = {USER: old_conn}
    pool._grace_tasks = {}
    pool.has_active_runs = MagicMock(return_value=False)
    pool.is_parked = MagicMock(return_value=False)

    close_calls = {"n": 0}

    async def fake_close(user_id):
        close_calls["n"] += 1
        pool._connections.pop(user_id, None)
        pool._grace_tasks.pop(user_id, None)
        if close_calls["n"] == 1:
            # A status-poll dial lands a replacement mid-drain.
            replacement = MagicMock()
            replacement.has_active_runs = False
            pool._connections[user_id] = replacement

    pool.close_user = fake_close

    first = asyncio.create_task(pool._grace_close(USER))
    await first
    # The exit re-arm must have armed a fresh cycle for the replacement.
    second = pool._grace_tasks.get(USER)
    assert second is not None and second is not first
    await second
    # Replacement got drained by its own cycle; nothing left, chain ended.
    assert close_calls["n"] == 2
    assert USER not in pool._connections
    assert USER not in pool._grace_tasks


@pytest.mark.asyncio
async def test_grace_stop_vetoed_by_replacement_connection():
    """At grace-path fire time the arming cycle already closed its own
    connection, so ANY connection present is a replacement dialed mid-sleep
    — the old cycle's deadline must not stop the container under it. The
    veto returns before any billing/ECS work."""
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {}
    replacement = MagicMock()
    pool._connections = {USER: replacement}

    with patch("core.containers.get_ecs_manager") as mock_get_ecs:
        await pool._maybe_stop_trial_container(USER, trigger="grace")
    mock_get_ecs.assert_not_called()


@pytest.mark.asyncio
async def test_grace_stop_vetoed_by_replacement_landing_mid_check():
    """TOCTOU closure: a replacement dial whose insert lands DURING the
    stop's awaited billing read must still veto — the final re-check under
    the pool lock runs immediately before the irreversible stop."""
    from core.repositories import billing_repo as billing_mod

    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value=None)
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    async def billing_read_inserts_replacement(owner_id):
        pool._connections[USER] = MagicMock()
        return {"subscription_status": "trial_local"}

    with (
        patch.object(billing_mod, "get_by_owner_id", side_effect=billing_read_inserts_replacement),
        patch("core.containers.get_ecs_manager") as mock_get_ecs,
    ):
        await pool._maybe_stop_trial_container(USER, trigger="grace")
    mock_get_ecs.assert_not_called()


@pytest.mark.asyncio
async def test_grace_stop_vetoed_by_cross_worker_replacement():
    """The locked local re-check is process-local; a replacement dialed on
    ANOTHER worker announces itself only via the durable gateway-liveness
    row. A fresh row at grace-fire time must veto the stop — the worker
    that dialed it owns teardown."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value="tok-replacement")
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    from core.repositories import billing_repo as billing_mod

    with (
        patch.object(
            billing_mod,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch("core.containers.get_ecs_manager") as mock_get_ecs,
    ):
        await pool._maybe_stop_trial_container(USER, trigger="grace", closed_conn_token="tok-old")
    mock_get_ecs.assert_not_called()
    conn_svc.get_live_gateway_conn_token.assert_awaited_once_with(USER)


@pytest.mark.asyncio
async def test_grace_stop_proceeds_over_own_stale_liveness_row():
    """A fresh row bearing the token of the connection THIS cycle closed is
    its own leftover (the conditioned delete failed) — NOT a replacement.
    Vetoing on it would leave the container running with nothing armed to
    ever re-check."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value="tok-old")
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    from core.repositories import billing_repo as billing_mod

    stop = AsyncMock()
    with (
        patch.object(
            billing_mod,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.containers.get_ecs_manager",
            return_value=MagicMock(stop_user_service=stop),
        ),
    ):
        await pool._maybe_stop_trial_container(USER, trigger="grace", closed_conn_token="tok-old")
    stop.assert_awaited_once_with(USER, reason="trial_idle")


@pytest.mark.asyncio
async def test_grace_stop_aborted_when_liveness_read_fails():
    """An unreadable liveness row means the replacement question is
    UNANSWERED — any doubt = don't stop. The getter propagates and the stop
    path's catch-all skips the stop; the next teardown cycle retries."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(side_effect=RuntimeError("ddb read failed"))
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    from core.repositories import billing_repo as billing_mod

    stop = AsyncMock()
    with (
        patch.object(
            billing_mod,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.containers.get_ecs_manager",
            return_value=MagicMock(stop_user_service=stop),
        ),
    ):
        await pool._maybe_stop_trial_container(USER, trigger="grace", closed_conn_token="tok-old")
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_boot_reconcile_vetoed_by_live_connection_on_another_worker():
    """A NEW worker's boot sweep is blind to connections existing workers
    hold — a fresh liveness row (any token; the sweep closed nothing) means
    the container is NOT orphaned and must not be stopped."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value="tok-live-elsewhere")
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    from core.repositories import billing_repo as billing_mod

    stop = AsyncMock()
    with (
        patch.object(
            billing_mod,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.containers.get_ecs_manager",
            return_value=MagicMock(stop_user_service=stop),
        ),
    ):
        await pool._maybe_stop_trial_container(USER, trigger="boot_reconcile")
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_grace_stop_vetoed_by_local_dial_landing_during_the_liveness_read():
    """A local dial can complete and insert into _connections while the
    awaited durable read is in flight (its own liveness write is async, so
    the strong read misses it too). The final synchronous local look must
    catch it."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    pool = GatewayConnectionPool(management_api=None, conn_svc=conn_svc)

    async def read_while_dial_lands(owner_id):
        pool._connections[USER] = MagicMock()
        return None

    conn_svc.get_live_gateway_conn_token = AsyncMock(side_effect=read_while_dial_lands)

    from core.repositories import billing_repo as billing_mod

    stop = AsyncMock()
    with (
        patch.object(
            billing_mod,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.containers.get_ecs_manager",
            return_value=MagicMock(stop_user_service=stop),
        ),
    ):
        await pool._maybe_stop_trial_container(USER, trigger="grace", closed_conn_token="tok-old")
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_fences_new_dials_and_snapshots_late_inserts():
    """close_all sets the closing flag and passes through the connect lock
    once: a dial that began before shutdown finishes inserting and is
    closed by the snapshot; a dial arriving after refuses. Without the
    fence, a mid-shutdown insert leaves a live reader and a fresh liveness
    row that other workers read as \"ready\" off a dead process."""
    from core.gateway.connection_pool import GatewayReconnectThrottled

    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._lock = asyncio.Lock()
    pool._connections = {}
    pool._frontend_connections = {}
    pool._grace_tasks = {}
    closed = []

    async def fake_close_user(uid):
        closed.append(uid)
        pool._connections.pop(uid, None)

    pool.close_user = fake_close_user
    pool._sender = MagicMock()
    pool._sender.flush = AsyncMock()
    drained = AsyncMock()

    # Simulate an in-flight dial: it holds the lock and inserts before
    # releasing — close_all must wait for it and include it in the snapshot.
    release = asyncio.Event()

    async def in_flight_dial():
        async with pool._lock:
            await release.wait()
            conn = MagicMock()
            conn.drain_liveness_write = drained
            pool._connections[USER] = conn

    dial = asyncio.create_task(in_flight_dial())
    await asyncio.sleep(0)  # dial acquires the lock
    shutdown = asyncio.create_task(pool.close_all())
    await asyncio.sleep(0)  # close_all sets the flag, blocks on the lock
    assert pool._closing is True
    release.set()
    await dial
    await shutdown
    assert closed == [USER]
    # The snapshot's connections get their scheduled liveness writes
    # drained — the terminal delete must land before shutdown returns.
    drained.assert_awaited_once()

    # And a dial arriving after the fence refuses outright.
    with pytest.raises(GatewayReconnectThrottled):
        await pool.ensure_connection(USER, "10.0.0.9", "tok")
