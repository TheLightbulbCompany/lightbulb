"""Human operator diagnostics for the platform-admin console."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.auth import AuthContext, require_platform_admin
from core.services.ops import alarms, deploys, fleet
from core.services.ops import envelope as env

router = APIRouter()


@router.get("/fleet")
async def get_fleet(_auth: AuthContext = Depends(require_platform_admin)):
    # Single live read: fleet_counts_live() derives from fleet_grid() internally,
    # so read the grid ONCE and compute counts locally — avoids a redundant
    # second ECS/scan fan-out and a compound timeout. On failure the whole read
    # degrades together (grid is None -> counts None -> source_state != ok).
    grid, state, errors = await env.read_source("ecs", fleet.fleet_grid)
    counts = fleet.counts_from_grid(grid) if grid is not None else None
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
    """Fleet growth metrics (the observability audit §6), computed on read, 60s cache.
    Partial sources surface in errors[] — the page must never render a
    missing section as zero-and-green."""
    from core.services.ops import growth

    data, state, errors = await growth.compute()
    return env.envelope(data, source_state=state, errors=errors)
