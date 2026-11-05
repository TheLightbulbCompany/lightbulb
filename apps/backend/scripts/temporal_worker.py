"""Temporal worker entrypoint: the `temporal-worker` ECS service runs the backend image with
`python -m scripts.temporal_worker`; the `temporal-watchdog` service runs it with `--queue lightbulb-watchdog`
(spec 11: its own queue and service, so the watchdog ticks when the main pool is stuck). Exits 1 when the
super-agent env master is off, or when `--queue` names neither configured queue.

Local recipe (the env master is the super-agent setting in core/config.py; only `flags_for`'s module
names it in code, `test_no_other_reader`):
    brew install temporal
    temporal server start-dev --db-filename "$SCRATCH/temporal.db"
    <env master>=true uv run python -m scripts.temporal_worker
"""

import argparse
import asyncio
import logging
import signal
import sys
from datetime import timedelta

from core.config import settings
from core.services.super_agent_flags import env_enabled

logger = logging.getLogger("temporal_worker")


PLATFORM_BOOT_TIMEOUT_S = 30


async def _ensure_platform_schedules(client) -> None:
    """Automation reconcile on boot + its daily platform Schedule (lane A). Bounded, so a hung Temporal call
    never holds up SIGTERM; a failure never stops the worker."""
    try:
        from core.services import automation_schedule

        await asyncio.wait_for(automation_schedule.ensure_platform(client), PLATFORM_BOOT_TIMEOUT_S)
    except Exception:
        logger.warning("platform schedules not ensured", exc_info=True)


async def run(queue: str | None = None) -> int:
    if not env_enabled():
        logger.error("super-agent env master is off; not starting the Temporal worker")
        return 1
    queue = queue or settings.TEMPORAL_TASK_QUEUE
    watchdog = queue == settings.TEMPORAL_WATCHDOG_TASK_QUEUE
    if not watchdog and queue != settings.TEMPORAL_TASK_QUEUE:
        # Loud, not a silent second main worker (which would also leave P48 alarming on a silent watchdog).
        logger.error(
            "unknown task queue %r (main=%r, watchdog=%r)",
            queue,
            settings.TEMPORAL_TASK_QUEUE,
            settings.TEMPORAL_WATCHDOG_TASK_QUEUE,
        )
        return 1
    from temporalio.worker import Worker

    from core.temporal.client import connect, set_client

    if watchdog:
        from core.services.watchdog import WATCHDOG_ACTIVITIES as activities
        from core.temporal.watchdog_workflow import WATCHDOG_WORKFLOWS as workflows
    else:
        from core.temporal.registry import ALL_ACTIVITIES as activities, ALL_WORKFLOWS as workflows

    client = await connect()
    set_client(client)  # activities reach workflows through get_client()
    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=activities,
        graceful_shutdown_timeout=timedelta(seconds=20),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        async with worker:  # exit awaits worker.shutdown(): in-flight activities get the graceful window
            logger.info("worker started", extra={"task_queue": queue})
            if watchdog:
                from core.services.watchdog import ensure_watchdog_schedule

                await ensure_watchdog_schedule(client)
            else:
                await _ensure_platform_schedules(client)
            await stop.wait()
            logger.info("worker stopping")
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", default=None, help="TEMPORAL_TASK_QUEUE (default) or TEMPORAL_WATCHDOG_TASK_QUEUE")
    return asyncio.run(run(parser.parse_args().queue))


if __name__ == "__main__":
    sys.exit(main())
