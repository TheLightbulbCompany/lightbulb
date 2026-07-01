"""Live-ECS fleet reads for the operator console.

Enumerates the fleet from the DDB container index, enriches with live
describe_services (≤10/call), maps to health, and serves through the
ops-cache (~20s TTL). Replaces the DDB-derived counts in system_health.
"""

from __future__ import annotations

import asyncio
import time

import boto3

from core.config import settings
from core.repositories import container_repo, ops_cache_repo

_FLEET_CACHE_TTL_S = 20
_DESCRIBE_SERVICES_BATCH = 10

_client = None


def _ecs():
    global _client
    if _client is None:
        _client = boto3.client("ecs", region_name=settings.AWS_REGION)
    return _client


def _cache_key() -> str:
    return f"fleet#{settings.ENVIRONMENT or 'dev'}"


def _classify(svc: dict, ddb_status: str) -> str:
    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)
    pending = svc.get("pendingCount", 0)
    primary = next(
        (d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"),
        None,
    )
    rollout = primary.get("rolloutState") if primary else None
    if ddb_status == "error" or rollout == "FAILED":
        return "error"
    if desired == 0:
        return "stopped"
    if rollout == "IN_PROGRESS" or pending > 0:
        return "provisioning"
    if rollout == "COMPLETED" and running >= desired >= 1:
        return "running"
    return "unknown"


async def _describe(service_names: list[str]) -> dict[str, dict]:
    """Return {service_name: service_dict} via batched describe_services."""
    out: dict[str, dict] = {}
    for i in range(0, len(service_names), _DESCRIBE_SERVICES_BATCH):
        batch = service_names[i : i + _DESCRIBE_SERVICES_BATCH]
        resp = await asyncio.to_thread(
            _ecs().describe_services,
            cluster=settings.ECS_CLUSTER_ARN,
            services=batch,
        )
        for svc in resp.get("services", []):
            out[svc.get("serviceName")] = svc
    return out


async def _build_grid() -> list[dict]:
    rows = await container_repo.scan_all()
    named = [r for r in rows if r.get("service_name")]
    services = await _describe([r["service_name"] for r in named])
    grid: list[dict] = []
    for r in named:
        svc = services.get(r["service_name"], {})
        # An indexed service absent from describe_services' returned services{}
        # (deleted, or in the ignored failures[]) is a describe gap, not real
        # state — classify it "unknown" rather than letting _classify read the
        # empty dict's desiredCount=0 and mask the gap as "stopped".
        if r["service_name"] not in services:
            status = "unknown"
        else:
            status = _classify(svc, r.get("status", ""))
        grid.append(
            {
                "owner_id": r.get("owner_id"),
                "service_name": r["service_name"],
                "status": status,
                "desired_count": svc.get("desiredCount", 0),
                "running_count": svc.get("runningCount", 0),
                "pending_count": svc.get("pendingCount", 0),
                "rollout_state": next(
                    (d.get("rolloutState") for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"),
                    None,
                ),
            }
        )
    return grid


async def fleet_grid() -> list[dict]:
    # Read-through with a short TTL, enforced on read: DynamoDB TTL deletion is
    # lazy (an item can linger hours past `ttl`), so honour the stored ttl here
    # rather than trusting the row's presence. Spec §5.3's decoupled refresher
    # (poll-and-cache off the render path) is the follow-up when the fleet grows;
    # P1 stays a read-through with a short TTL.
    cached = await ops_cache_repo.get(_cache_key())
    now = int(time.time())
    if cached is not None and "value" in cached and int(cached.get("ttl", 0)) > now:
        return cached["value"]
    grid = await _build_grid()
    await ops_cache_repo.put(_cache_key(), grid, ttl_seconds=_FLEET_CACHE_TTL_S)
    return grid


def counts_from_grid(grid: list[dict]) -> dict[str, int]:
    """Aggregate status counts from an already-built fleet grid.

    Pure (no I/O) so a caller that already has the grid (e.g. the /ops/fleet
    router) can derive counts without a second live read.
    """
    counts = {"running": 0, "provisioning": 0, "stopped": 0, "error": 0, "unknown": 0}
    for g in grid:
        status = g.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = sum(counts.values())
    return counts


async def fleet_counts_live() -> dict[str, int]:
    return counts_from_grid(await fleet_grid())
