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
