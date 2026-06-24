"""Teams BFF — Activity + Costs + Dashboard.

All three are pure-read aggregation surfaces. Dashboard fans out
into two upstream calls (the dashboard summary and the sidebar
badge counts) and merges them into one response so the panel
renders in a single round-trip.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext

router = APIRouter()
_ctx = _agents._ctx


@router.get("/activity")
async def list_activity(
    ctx: TeamsContext = Depends(_ctx),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    """List company activity events.

    Forwards ``limit`` to upstream (bounded 1–500, upstream's
    ``MAX_ACTIVITY_LIMIT``). The Activity feed asks for 200; without
    forwarding it the feed silently caps at upstream's default of 100.
    """
    return await _agents._admin().list_activity(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        limit=limit,
    )


@router.get("/costs")
async def get_costs(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the company costs summary."""
    return await _agents._admin().get_costs(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/dashboard")
async def get_dashboard(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the dashboard summary + sidebar badges in parallel.

    Returns the upstream Paperclip dashboard summary verbatim
    (``agents`` / ``tasks`` / ``runActivity`` / ``costs`` /
    ``pendingApprovals`` / ``budgets``) so the panel can render the
    full set of metric tiles + charts without further fan-out.

    Defensive: if upstream returns None or a non-dict, normalize to
    an empty dict so the frontend can still render an empty-state.
    """
    admin = _agents._admin()
    badges, dash = await asyncio.gather(
        admin.get_sidebar_badges(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
        admin.get_dashboard(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
    )
    return {
        "dashboard": dash if isinstance(dash, dict) else {},
        "sidebar_badges": badges,
    }


@router.get("/sidebar-badges")
async def get_sidebar_badges(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the sidebar badge counts on their own.

    Exposed as a separate endpoint (in addition to being aggregated
    into ``/dashboard``) because the global app shell polls badges
    on a faster cadence than the dashboard panel itself.
    """
    return await _agents._admin().get_sidebar_badges(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
