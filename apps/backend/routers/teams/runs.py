"""Teams BFF — Heartbeat Runs.

Surfaces upstream Paperclip's heartbeat-run detail (run row, log slice,
event timeline) plus the cancel action. Used by the agent-run page that
Inbox failed-run rows link into. Reuses the shared ``_ctx`` Depends
helper so auth + session cookie plumbing is consistent across the
Teams BFF.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import WatchdogDecisionBody

router = APIRouter()
_ctx = _agents._ctx

# Default tail size for the live-run transcript read. Kept small so the
# Dashboard cards stay glanceable (a recent-activity snippet, not a full
# scrollback). Upstream's own clamp ceiling is 1 MiB; we cap our public
# param well below that so a card poll can never pull a megabyte.
RUN_LOG_DEFAULT_LIMIT_BYTES = 65_536
RUN_LOG_MAX_LIMIT_BYTES = 262_144


@router.get("/heartbeat-runs/{run_id}")
async def get_run(run_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single heartbeat-run by id.

    Maps to upstream ``GET /api/heartbeat-runs/{runId}``. Distinct from
    the legacy ``GET /teams/runs/{id}`` (in ``agents.py``) which targets
    upstream's generic ``/api/runs/{id}``.
    """
    return await _agents._admin().get_heartbeat_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/heartbeat-runs/{run_id}/log")
async def get_run_log(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit_bytes: int = Query(
        RUN_LOG_DEFAULT_LIMIT_BYTES,
        ge=1,
        le=RUN_LOG_MAX_LIMIT_BYTES,
        alias="limitBytes",
    ),
    ctx: TeamsContext = Depends(_ctx),
):
    """Read a slice of a heartbeat run's raw log (the live transcript).

    Maps to upstream ``GET /api/heartbeat-runs/{runId}/log``. Returns the
    upstream envelope verbatim: ``{runId, store, logRef, content,
    nextOffset?}``, where ``content`` is the log slice from ``offset`` for
    up to ``limitBytes`` bytes. The Dashboard live-run cards poll this for
    the in-progress "Working… / Executing command / …" activity and render
    the last N lines of ``content``.
    """
    return await _agents._admin().get_run_log(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get("/heartbeat-runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0, alias="afterSeq"),
    limit: int = Query(200, ge=1, le=1000),
    ctx: TeamsContext = Depends(_ctx),
):
    """List the structured event timeline for a heartbeat run.

    Maps to upstream ``GET /api/heartbeat-runs/{runId}/events``. Upstream
    responds with a raw array; wrap it in the standard list envelope so the
    frontend reads ``data.events`` like every other Teams list surface.
    """
    rows = await _agents._admin().get_run_events(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
        after_seq=after_seq,
        limit=limit,
    )
    return envelope_list(rows, "events")


@router.post("/heartbeat-runs/{run_id}/cancel")
async def cancel_run(run_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Cancel a queued/running heartbeat run.

    Maps to upstream ``POST /api/heartbeat-runs/{runId}/cancel``. Stopping a
    run never *starts* spend, so no trial-runway gate here (unlike wakeup).
    """
    return await _agents._admin().cancel_heartbeat_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/heartbeat-runs/{run_id}/watchdog-decisions")
async def record_watchdog_decision(
    run_id: str,
    body: WatchdogDecisionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Record a watchdog decision (continue / snooze / false-positive) for a
    silent run. Maps to upstream ``POST /api/heartbeat-runs/{runId}/watchdog-decisions``."""
    return await _agents._admin().record_watchdog_decision(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )
