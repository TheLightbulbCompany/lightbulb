"""Update service -- pending-update applier + scheduled worker.

Plus the P0c owner-day rollup pass (``_rollup_pass``) — once per hour,
refresh today+yesterday rollup rows for every owner with a bound company
Stripe account (``rollup_service.refresh_owner_day``).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo

logger = logging.getLogger(__name__)

# Hourly owner-day rollup pass cadence, in seconds (P0c).
_ROLLUP_PASS_INTERVAL_SECONDS = 60 * 60

# Inter-iteration wait for run_scheduled_worker, in seconds.
_WORKER_INTERVAL_S = 60


# Cooperative stop signal for run_scheduled_worker. Created LAZILY inside
# run_scheduled_worker so the Event binds to the loop that actually runs the
# worker. A module-level asyncio.Event() binds to the import-time loop and
# raises "bound to a different event loop" under anything that spins up a
# fresh loop (e.g. the ASGI test client per contract test). request_worker_stop
# lets a graceful shutdown ask the worker to finish its current iteration and
# exit instead of hard-cancelling mid-pass.
_worker_stop: asyncio.Event | None = None


def request_worker_stop() -> None:
    """Ask run_scheduled_worker to finish its current iteration and exit."""
    if _worker_stop is not None:
        _worker_stop.set()


async def _rollup_pass() -> None:
    """P0c: refresh today+yesterday owner-day rollup rows.

    Iterates owners with a bound company Stripe account and recomputes
    their rollup rows from provider truth. Per-owner try/except so one
    owner's provider failure never starves the rest. Idempotent by
    construction (whole-row recompute-and-replace), so the transient
    ×2 worker during deploys is harmless.
    """
    from core.services import rollup_service  # local: keeps worker import light

    owners = await billing_repo.list_company_stripe_owners()
    today = datetime.now(timezone.utc).date()
    days = (today.isoformat(), (today - timedelta(days=1)).isoformat())
    for owner_id in owners:
        try:
            for day in days:
                await rollup_service.refresh_owner_day(owner_id, day)
        except Exception:
            logger.exception("rollup refresh failed (owner=%s)", owner_id)


async def admitted(name: str, *, interval_s: int) -> bool:
    """Pass admission plus its metric. See container_repo.try_claim_pass."""
    won = await container_repo.try_claim_pass(name, interval_s=interval_s)
    put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "won" if won else "lost"})
    return won


async def run_scheduled_worker() -> None:
    """Background worker for the three fleet-wide reconciliation passes.

    Runs every 60 seconds indefinitely. Each iteration enters its passes (owner-day rollup) only on the instance that wins ``admitted()`` for that
    pass's window that tick -- at desiredCount >= 2 the loser scans nothing, instead of every
    instance re-scanning the same rows. Cadence for the hourly rollup lives on
    the admission row (``interval_s``), not a process-local clock, so a deploy no longer forces
    an immediate run.

    The fourth pass -- applying due Track-2 container updates (image rolls, resizes,
    task-definition refreshes) and recovering their stranded claims -- went with the ECS
    runtime. Only the heartbeat metric below is deliberately un-admitted (this loop's own
    liveness canary).
    """
    logger.info("Scheduled update worker started")
    global _worker_stop
    _worker_stop = asyncio.Event()  # bind to the loop running this worker
    while not _worker_stop.is_set():
        try:
            put_metric("update.scheduled_worker.heartbeat")
            # P0c: owner-day rollup pass — admitted once per
            # _ROLLUP_PASS_INTERVAL_SECONDS (1h), fleet-wide.
            if await admitted("owner_day_rollup", interval_s=_ROLLUP_PASS_INTERVAL_SECONDS):
                try:
                    await _rollup_pass()
                except Exception:
                    logger.exception("rollup_pass crashed")
        except Exception:
            put_metric("update.scheduled_worker.error")
            logger.exception("Error in scheduled update worker loop")

        # Interruptible inter-iteration wait: wakes immediately on stop request.
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=_WORKER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
