import asyncio
from unittest.mock import AsyncMock

import pytest

from core.services import update_service as us


@pytest.mark.asyncio
async def test_worker_exits_cleanly_on_stop_without_cancel(monkeypatch):
    iterations = {"n": 0}

    async def _fake_pass():
        iterations["n"] += 1

    # Make each loop iteration cheap + the inter-iteration wait short.
    monkeypatch.setattr(us, "_paperclip_provision_retry_pass", _fake_pass, raising=False)
    monkeypatch.setattr(us, "_WORKER_INTERVAL_S", 0.05, raising=False)
    # Stub the legacy due-update path so the loop body reaches the retry pass
    # without hitting real DynamoDB (mirrors the worker tests in
    # test_update_service.py).
    monkeypatch.setattr(us, "_paperclip_purge_pass", AsyncMock(), raising=False)
    monkeypatch.setattr(us, "_rollup_pass", AsyncMock(), raising=False)
    monkeypatch.setattr(us.update_repo, "get_due_scheduled", AsyncMock(return_value=[]))

    us.reset_worker_stop()
    task = asyncio.create_task(us.run_scheduled_worker())
    await asyncio.sleep(0.12)
    us.request_worker_stop()
    # Must finish on its own (no .cancel()) within a bounded wait.
    await asyncio.wait_for(task, timeout=1.0)
    assert iterations["n"] >= 1
