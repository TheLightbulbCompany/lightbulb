"""Teams BFF — Heartbeat Runs.

Read-only resource that surfaces upstream Paperclip's heartbeat-run
detail. Used by the agent-run page that Inbox failed-run rows link into.
Reuses the shared ``_ctx`` Depends helper so auth + session cookie
plumbing is consistent across the Teams BFF.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext

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
