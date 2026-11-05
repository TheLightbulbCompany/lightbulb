"""Review focus 5 (wave-1 plan T): the temporal-worker task refuses to run with the env master off, and a
SIGTERM (ECS deploy or Spot reclaim) shuts the worker down gracefully inside the 30 s stopTimeout."""

import asyncio
import os
import signal
import time
from unittest.mock import AsyncMock

from core.config import settings
from core.temporal import client as tc
from scripts import temporal_worker


async def test_worker_refuses_when_env_master_off(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)
    connect = AsyncMock()
    monkeypatch.setattr(tc, "connect", connect)
    assert await temporal_worker.run() != 0
    connect.assert_not_awaited()


async def test_worker_sigterm_shuts_down_gracefully(monkeypatch, temporal_env):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    monkeypatch.setattr(settings, "TEMPORAL_TASK_QUEUE", "test-sigterm")
    monkeypatch.setattr(tc, "connect", AsyncMock(return_value=temporal_env.client))
    started = asyncio.Event()
    real_info = temporal_worker.logger.info

    def info(msg, *args, **kwargs):
        real_info(msg, *args, **kwargs)
        if msg == "worker started":
            started.set()

    monkeypatch.setattr(temporal_worker.logger, "info", info)
    task = asyncio.create_task(temporal_worker.run())
    await asyncio.wait_for(started.wait(), 20)  # the handler is installed before this line logs

    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 25) == 0
    assert time.monotonic() - t0 < 25
    # run() takes its handlers down with it: nothing left that swallows a later SIGTERM
    assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False
