"""Human operator diagnostics for the platform-admin console."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.auth import AuthContext, require_platform_admin
from core.services.ops import alarms, deploys, ecs_ops
from core.services.ops import envelope as env

router = APIRouter()


@router.get("/fleet")
async def get_fleet(_auth: AuthContext = Depends(require_platform_admin)):
    # Single live read: fleet_counts_live() derives from fleet_grid() internally,
    # so read the grid ONCE and compute counts locally — avoids a redundant
    # second ECS/scan fan-out and a compound timeout. On failure the whole read
    # degrades together (grid is None -> counts None -> source_state != ok).
    grid, state, errors = await env.read_source("ecs", ecs_ops.fleet_grid)
    counts = ecs_ops.counts_from_grid(grid) if grid is not None else None
    return env.envelope({"counts": counts, "grid": grid}, source_state=state, errors=errors)


@router.get("/deploys")
async def get_deploys(_auth: AuthContext = Depends(require_platform_admin)):
    data, state, errors = await env.read_source("cloudformation", deploys.list_stacks)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.get("/alarms")
async def get_alarms(_auth: AuthContext = Depends(require_platform_admin)):
    data, state, errors = await env.read_source("cloudwatch", alarms.alarms_in_alarm)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.get("/growth")
async def get_growth(_auth: AuthContext = Depends(require_platform_admin)):
    """Fleet GTM metrics (docs/growth §3), computed on read, 60s cache.
    Partial sources surface in errors[] — the page must never render a
    missing section as zero-and-green."""
    from core.services.ops import growth

    data, state, errors = await growth.compute()
    return env.envelope(data, source_state=state, errors=errors)


@router.get("/launch")
async def get_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Launch Competition — the economics sheet with live inputs, 60s cache."""
    from core.services.ops import launch as launch_ops

    data, state, errors = await launch_ops.compute()
    return env.envelope(data, source_state=state, errors=errors)


def _clear_public_launch_cache() -> None:
    """The admin console's cache is not the only one — /launch and
    /launch/leaderboard hold their own 20-30s response cache. Drop it too, so a
    taken-down entrant leaves the public board now rather than up to 30s later.
    Lazy import: routers.launch imports launch services this module doesn't."""
    from routers.launch import clear_response_cache

    clear_response_cache()


@router.post("/launch/refresh")
async def refresh_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Force-refresh every entrant's revenue now (judging day)."""
    from core.services import launch_revenue
    from core.services.ops import launch as launch_ops

    n = await launch_revenue.refresh_all(force=True)
    launch_ops.clear_cache()
    _clear_public_launch_cache()
    return {"refreshed": n}


@router.delete("/launch/entries/{owner_id}")
async def delete_launch_entry(owner_id: str, _auth: AuthContext = Depends(require_platform_admin)):
    """Take an entry off the public board and revoke its stored Stripe key.

    The only removal door — self-serve key revocation is a v1 non-goal, so a
    fraudulent or withdrawn entrant is removed here."""
    from core.repositories import launch_entries_repo
    from core.services import launch_revenue
    from core.services.ops import launch as launch_ops

    # Key first: if the row delete below fails partway, a dangling Secrets
    # Manager entry with no owning row is worse than a row that still has
    # has_key=true pointing at an already-revoked key.
    await launch_revenue.delete_key(owner_id)
    await launch_entries_repo.delete(owner_id)
    launch_ops.clear_cache()
    _clear_public_launch_cache()
    return {"deleted": owner_id}
