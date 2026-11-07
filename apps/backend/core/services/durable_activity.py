"""Periodic trial scale-to-zero from durable command activity, never UI presence."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from core.dynamodb import plain
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services.connection_service import ConnectionService

logger = logging.getLogger(__name__)

TRIAL_IDLE_SECONDS = 180
_IDLE_CLAIM_LEASE_SECONDS = 300
_SWEEP_SECONDS = 60


def _activity_epoch(row: dict) -> int:
    # DynamoDB never hands back a numeric attribute as int/float -- boto3's
    # resource API deserializes every `N` as decimal.Decimal, which is NOT a
    # numbers.Real/int/float instance. `plain()` is the repo's existing
    # Decimal -> int/float converter (core/dynamodb.py); without it this
    # check silently missed every real last_interactive_at and fell through
    # to the updated_at/created_at fallback below, disabling trial
    # scale-to-zero fleet-wide.
    value = plain(row.get("last_interactive_at"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    # No recorded interaction ever (legacy row, or nobody has messaged this
    # container yet): fall back to created_at only, never updated_at.
    # updated_at is a generic write timestamp touched by unrelated writers
    # (gate-token swaps, task-def refreshes) -- and,
    # self-referentially, by this sweep's OWN try_claim_idle_stop claim. A
    # row without last_interactive_at would otherwise never go idle: each
    # claim attempt re-stamps updated_at, which this function would then read
    # back as "just active", vetoing the very claim that wrote it.
    # created_at never changes after the row is created, so it carries no
    # such feedback loop.
    raw = row.get("created_at")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


async def sweep_trial_idle_once(*, now: int | None = None) -> None:
    now = int(now if now is not None else time.time())
    rows = await container_repo.get_by_status("running")
    connections = ConnectionService()
    for row in rows:
        owner_id = row.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id:
            continue
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        if not account or account.get("subscription_status") != "trial_local":
            continue
        try:
            from core.services.trial_lifecycle import notices

            await notices(owner_id, account, now)
        except Exception:
            logger.exception("trial lifecycle notice failed for %s", owner_id)
        activity = _activity_epoch(row)
        if activity > now - TRIAL_IDLE_SECONDS:
            continue
        if await messaging_repo.has_open_runs(owner_id):
            continue
        if await connections.has_live_operator_connection(owner_id):
            continue
        fresh = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not fresh or _activity_epoch(fresh) > now - TRIAL_IDLE_SECONDS:
            continue
        observed = fresh.get("last_interactive_at")
        claim = await container_repo.try_claim_idle_stop(
            owner_id,
            observed_last_interactive_at=int(observed) if observed is not None else None,
            stale_claim_before=now - _IDLE_CLAIM_LEASE_SECONDS,
        )
        if not claim or not await container_repo.idle_stop_claim_current(owner_id, claim):
            continue
        # Re-run every mutable veto adjacent to the irreversible ECS stop. Any
        # accepted command clears the claim, and paid/work/gateway state can move
        # after the first pass.
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        if not account or account.get("subscription_status") != "trial_local":
            continue
        if await messaging_repo.has_open_runs(owner_id):
            continue
        if await connections.has_live_operator_connection(owner_id):
            continue
        fresh = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not fresh or _activity_epoch(fresh) > now - TRIAL_IDLE_SECONDS:
            continue
        if not await container_repo.idle_stop_claim_current(owner_id, claim):
            continue
        # Nothing to stop: AWS parks an idle microVM on its own idle timeout, and an
        # AgentCore runtime has no desired count to scale. The sweep survives as the
        # claim + the metric -- the record of "this workspace went idle" that
        # `wake_policy` and the growth dashboards read.
        put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "durable_activity"})


async def admitted(name: str, *, interval_s: int) -> bool:
    """Pass admission plus its metric. See container_repo.try_claim_pass.

    A local copy of update_service.admitted, not an import: each admission
    point emits through its OWN module's put_metric so it stays independently
    patchable in tests, same shape either way.
    """
    won = await container_repo.try_claim_pass(name, interval_s=interval_s)
    put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "won" if won else "lost"})
    return won


async def run_trial_idle_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await admitted("trial_idle", interval_s=_SWEEP_SECONDS):
                await sweep_trial_idle_once()
        except Exception:
            put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "sweep_error"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
