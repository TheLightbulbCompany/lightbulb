"""arm_idle_teardown_if_unattended: the self-heal dial's teardown hook.

``remove_frontend_connection`` is otherwise the only armer of ``_grace_close``
— it fires on a frontend LEAVING. A gateway connection created while the
frontend set is already empty (the status poll's self-heal dial) never
traverses that path, so without this hook nothing would ever close it and a
trial container would stay running with nobody attached.
"""

import asyncio
from unittest.mock import MagicMock

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
async def test_noop_when_grace_task_already_pending():
    async def _pending():
        await asyncio.sleep(3600)

    pending = asyncio.create_task(_pending())
    pool = _pool(grace_task=pending)
    pool.arm_idle_teardown_if_unattended(USER)
    assert pool._grace_tasks[USER] is pending
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
