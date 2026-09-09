import asyncio

import pytest

from core.config import settings
from core.services.owner_lifecycle import owner_lifecycle_guard


@pytest.mark.asyncio
async def test_child_task_cannot_inherit_parent_owner_lock(tmp_path, monkeypatch):
    """A spawned replacement/provision task must wait for its parent to release."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    child_started = asyncio.Event()
    child_entered = asyncio.Event()

    async def child():
        child_started.set()
        async with owner_lifecycle_guard("org_one"):
            child_entered.set()

    async with owner_lifecycle_guard("org_one"):
        task = asyncio.create_task(child())
        await child_started.wait()
        await asyncio.sleep(0)
        assert not child_entered.is_set()

    await task
    assert child_entered.is_set()


@pytest.mark.asyncio
async def test_same_task_can_reenter_same_owner_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    async with owner_lifecycle_guard("org_one"):
        async with owner_lifecycle_guard("org_one"):
            pass
