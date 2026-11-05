"""Temporal worker entrypoint: the `temporal-worker` ECS service runs the backend image with
`python -m scripts.temporal_worker`. Exits 1 when the super-agent env master is off.

Local recipe (the env master is the super-agent setting in core/config.py; only `flags_for`'s module
names it in code, `test_no_other_reader`):
    brew install temporal
    temporal server start-dev --db-filename "$SCRATCH/temporal.db"
    <env master>=true uv run python -m scripts.temporal_worker
"""

import asyncio
import logging
import signal
import sys
from datetime import timedelta

from core.config import settings
from core.services.super_agent_flags import env_enabled

logger = logging.getLogger("temporal_worker")


async def run() -> int:
    if not env_enabled():
        logger.error("super-agent env master is off; not starting the Temporal worker")
        return 1
    from temporalio.worker import Worker

    from core.temporal.client import connect
    from core.temporal.registry import ALL_ACTIVITIES, ALL_WORKFLOWS

    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
        graceful_shutdown_timeout=timedelta(seconds=20),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        async with worker:  # exit awaits worker.shutdown(): in-flight activities get the graceful window
            logger.info("worker started", extra={"task_queue": settings.TEMPORAL_TASK_QUEUE})
            await stop.wait()
            logger.info("worker stopping")
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
