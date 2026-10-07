"""The one clock in the turn contract: close runs that never started or stopped
heartbeating (spec §5). Reports; never re-runs."""

from __future__ import annotations

import asyncio
import logging
import time

from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services.messaging import post_run_failure_message, publish_run_error_frames

logger = logging.getLogger(__name__)

_SWEEP_SECONDS = 60


async def sweep_runs_once() -> int:
    now = int(time.time())
    closed = 0
    for ref in await messaging_repo.list_overdue_runs(now):
        # One poisoned row must not cost the rest of the tick.
        try:
            row = await messaging_repo.get_run(ref["ownerId"], ref["runId"])
            if row is None or row.get("status") == "ended" or int(row.get("deadlineAt", 0)) >= now:
                continue  # closed or extended between the index read and this consistent read
            error_kind = "agent_did_not_start" if row["status"] == "dispatched" else "agent_stopped_responding"
            ended = await messaging_repo.end_run(row["ownerId"], row["runId"], outcome="error", error_kind=error_kind)
            if ended is None:
                continue
            closed += 1
            put_metric(
                "messaging.run.ended",
                dimensions={"outcome": "error", "event_kind": ended.get("eventKind") or "unknown"},
            )
            put_metric("messaging.run.swept", dimensions={"error": error_kind})
            await publish_run_error_frames(ended, error_kind)
            await post_run_failure_message(row["ownerId"], ended, "error", error_kind)
        except Exception:
            put_metric("messaging.run.swept", dimensions={"error": "candidate_error"})
            logger.warning("sweeper candidate failed", exc_info=True)
            continue
    return closed


async def admitted(name: str, *, interval_s: int) -> bool:
    """Pass admission plus its metric. See container_repo.try_claim_pass.

    A local copy of update_service.admitted, not an import: each admission
    point emits through its OWN module's put_metric so it stays independently
    patchable in tests, same shape either way.
    """
    won = await container_repo.try_claim_pass(name, interval_s=interval_s)
    put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "won" if won else "lost"})
    return won


async def run_ledger_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            # end_run's conditional (messaging_repo.py:1685) remains the
            # correctness boundary; admission only stops the duplicate GSI query.
            if await admitted("run_ledger", interval_s=_SWEEP_SECONDS):
                await sweep_runs_once()
        except Exception:
            put_metric("messaging.run.swept", dimensions={"error": "sweep_error"})
            logger.warning("run ledger sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
