"""Fleet reads for the operator console.

Enumerates the fleet from the DDB container index, enriches each row with the owner's
display name, and serves from a short in-process TTL cache.

The row IS the state now. This used to enrich each row with a live
``describe_services`` (batched 10/call) and derive health from desired/running/pending
counts and the rollout state; there are no per-user services left to describe, and a
workspace's status lives on the row the provisioner writes. Idleness is deliberately NOT
reported here: AWS parks a microVM invisibly, so `wake_policy.is_parked_trial` (elapsed
time since `last_interactive_at`) is the only honest signal and it is per-owner, not a
fleet column.
"""

from __future__ import annotations

import asyncio
import time

from core.repositories import container_repo, user_repo
from core.services import clerk_admin

_FLEET_CACHE_TTL_S = 20

# Owner display names come from DDB/Clerk, both slower and rate-limited
# relative to the 20s overview auto-refresh — so resolutions live in a long
# in-process TTL cache and each rebuild's fetch fan-out is hard-bounded.
# Failures (and budget misses) are cached as None for the full TTL so a
# flapping Clerk is not re-hit on every refresh.
_NAME_CACHE_TTL_S = 600
_NAME_FETCH_BUDGET_S = 3.0
_NAME_FETCH_CONCURRENCY = 4
_name_cache: dict[str, tuple[float, str | None]] = {}

_fleet_cache: tuple[float, list[dict]] | None = None  # (expires_at_monotonic, grid)

_client = None


def _clerk_user_email(user: dict) -> str | None:
    primary_id = user.get("primary_email_address_id")
    emails = [e for e in user.get("email_addresses") or [] if e.get("email_address")]
    for e in emails:
        if e.get("id") == primary_id:
            return e["email_address"]
    return emails[0]["email_address"] if emails else None


async def _fetch_display_name(owner_id: str) -> str | None:
    """Best-effort owner_id → human label: user_ → email, org_ → org name."""
    if owner_id.startswith("user_"):
        # The users table stores email best-effort (older rows predate the
        # column) — cheap DDB read first, Clerk only on a miss.
        row = await user_repo.get(owner_id)
        if row and row.get("email"):
            return row["email"]
        user = await clerk_admin.get_user(owner_id)
        return _clerk_user_email(user) if user else None
    if owner_id.startswith("org_"):
        org = await clerk_admin.get_organization(owner_id)
        return (org or {}).get("name") or None
    return None


async def _display_names(owner_ids: list[str]) -> dict[str, str | None]:
    """Resolve owner display names through the module TTL cache.

    Never raises: per-owner failures resolve to None, and the whole miss
    fan-out is capped by a semaphore + one overall budget so a slow upstream
    cannot stall the fleet read.
    """
    now = time.monotonic()
    names: dict[str, str | None] = {}
    misses: list[str] = []
    for oid in dict.fromkeys(o for o in owner_ids if o):
        hit = _name_cache.get(oid)
        if hit is not None and now - hit[0] < _NAME_CACHE_TTL_S:
            names[oid] = hit[1]
        else:
            names[oid] = None
            misses.append(oid)
    if not misses:
        return names

    sem = asyncio.Semaphore(_NAME_FETCH_CONCURRENCY)

    async def _one(oid: str) -> None:
        async with sem:
            try:
                names[oid] = await _fetch_display_name(oid)
            except Exception:  # noqa: BLE001 — enrichment must never break the grid
                names[oid] = None

    try:
        await asyncio.wait_for(asyncio.gather(*(_one(o) for o in misses)), timeout=_NAME_FETCH_BUDGET_S)
    except Exception:  # noqa: BLE001 — budget miss → unresolved owners stay None
        pass
    ts = time.monotonic()
    for oid in misses:
        _name_cache[oid] = (ts, names[oid])
    return names


async def _build_grid() -> list[dict]:
    from core.services.runtime_transport import runtime_arn

    rows = [r for r in await container_repo.scan_all() if r.get("owner_id")]
    display_names = await _display_names([r["owner_id"] for r in rows])
    return [
        {
            "owner_id": r["owner_id"],
            "display_name": display_names.get(r["owner_id"]),
            "runtime_arn": runtime_arn(r),
            # The row's own status, unmapped: the provisioner writes
            # provisioning/running/error/stopped and nothing else can observe a truer
            # value. An unprovisioned row (no runtime ARN yet) reads "unknown" rather
            # than borrowing "stopped" from an absent service.
            "status": (r.get("status") or "unknown") if runtime_arn(r) else "unknown",
        }
        for r in rows
    ]


async def fleet_grid() -> list[dict]:
    # In-process read-through with a short TTL. Per-instance (not shared
    # across backend tasks) — redundant refetches are fine for an admin
    # surface on a 20s window. Spec §5.3's decoupled refresher
    # (poll-and-cache off the render path) is the follow-up when the fleet
    # grows; P1 stays a read-through with a short TTL.
    global _fleet_cache
    if _fleet_cache is not None and _fleet_cache[0] > time.monotonic():
        return _fleet_cache[1]
    grid = await _build_grid()
    # TTL anchored after the rebuild so a slow build doesn't eat the window.
    _fleet_cache = (time.monotonic() + _FLEET_CACHE_TTL_S, grid)
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
