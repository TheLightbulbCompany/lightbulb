"""arm_idle_teardown_if_unattended: the self-heal dial's teardown hook.

``remove_frontend_connection`` is otherwise the only armer of ``_grace_close``
— it fires on a frontend LEAVING. A gateway connection created while the
frontend set is already empty (the status poll's self-heal dial) never
traverses that path, so without this hook nothing would ever close it and a
trial container would stay running with nobody attached.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
async def test_pending_grace_task_is_cancelled_and_replaced():
    """A pending grace task may belong to the PREVIOUS connection (parked in
    the post-close trial-stop sleep) — it is not coverage for the connection
    that exists now. The arm must cancel it and install a fresh full cycle,
    or a vetoed stop on the stale task exits and leaves the replacement
    connection unarmed forever."""

    async def _pending():
        await asyncio.sleep(3600)

    pending = asyncio.create_task(_pending())
    pool = _pool(grace_task=pending)
    pool.arm_idle_teardown_if_unattended(USER)
    replacement = pool._grace_tasks[USER]
    assert replacement is not pending
    assert pending.cancelling() or pending.cancelled()
    await replacement
    with pytest.raises(asyncio.CancelledError):
        await pending


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
async def test_cancel_mid_close_still_completes_cleanup(monkeypatch):
    """Cancelling a grace task while it is INSIDE close_user (the
    cancel-and-replace race) must not abandon the old connection's drain:
    _grace_close shields the close, so the reader task and WebSocket are
    always torn down even though the grace task itself is interrupted."""
    from core.gateway import connection_pool as cp
    from core.repositories import billing_repo

    monkeypatch.setattr(cp, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=None))

    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {}
    conn = MagicMock()
    conn.has_active_runs = False
    pool._connections = {USER: conn}
    pool._grace_tasks = {}
    pool.has_active_runs = MagicMock(return_value=False)
    pool.is_parked = MagicMock(return_value=False)

    release = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def slow_close(user_id):
        await release.wait()
        cleanup_done.set()

    pool.close_user = slow_close

    task = asyncio.create_task(pool._grace_close(USER))
    # Let the task reach the shielded close_user and block on `release`.
    for _ in range(10):
        await asyncio.sleep(0)
    assert not cleanup_done.is_set()

    task.cancel()
    await task  # CancelledError is swallowed by _grace_close
    # The grace task is gone, but the shielded cleanup must still finish.
    assert not cleanup_done.is_set()
    release.set()
    await asyncio.wait_for(cleanup_done.wait(), timeout=2)
