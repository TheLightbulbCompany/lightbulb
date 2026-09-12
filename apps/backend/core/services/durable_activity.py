"""Periodic trial scale-to-zero from durable command activity, never UI presence."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from core.containers import get_ecs_manager, get_gateway_pool
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services.connection_service import ConnectionService

TRIAL_IDLE_SECONDS = 180
_IDLE_CLAIM_LEASE_SECONDS = 300
_SWEEP_SECONDS = 60


def _activity_epoch(row: dict) -> int:
    value = row.get("last_interactive_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    for field in ("updated_at", "created_at"):
        raw = row.get(field)
        if isinstance(raw, str):
            try:
                return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    return int(time.time())


async def sweep_trial_idle_once(*, now: int | None = None) -> None:
    now = int(now if now is not None else time.time())
    rows = await container_repo.get_by_status("running")
    pool = get_gateway_pool()
    connections = ConnectionService()
    for row in rows:
        owner_id = row.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id:
            continue
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        if not account or account.get("subscription_status") != "trial_local":
            continue
        activity = _activity_epoch(row)
        if activity > now - TRIAL_IDLE_SECONDS:
            continue
        if await messaging_repo.has_open_runs(owner_id) or pool.has_active_runs(owner_id) or pool.is_parked(owner_id):
            continue
        if await connections.has_live_operator_connection(owner_id):
            continue
        if not await pool.close_idle_user(owner_id):
            continue
        # A different backend task may still consume this owner's gateway events.
        if await connections.get_live_gateway_conn_token(owner_id) is not None:
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
        if await messaging_repo.has_open_runs(owner_id) or pool.has_active_runs(owner_id) or pool.is_parked(owner_id):
            continue
        if await connections.has_live_operator_connection(owner_id):
            continue
        if await connections.get_live_gateway_conn_token(owner_id) is not None:
            continue
        fresh = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not fresh or _activity_epoch(fresh) > now - TRIAL_IDLE_SECONDS:
            continue
        if not await container_repo.idle_stop_claim_current(owner_id, claim):
            continue
        await get_ecs_manager().stop_user_service(owner_id, reason="trial_idle")
        put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "durable_activity"})


async def run_trial_idle_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await sweep_trial_idle_once()
        except Exception:
            put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "sweep_error"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
