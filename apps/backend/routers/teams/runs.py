"""Teams BFF — Heartbeat Runs.

Surfaces upstream Paperclip's heartbeat-run detail (run row, log slice,
event timeline) plus the cancel action. Used by the agent-run page that
Inbox failed-run rows link into. Reuses the shared ``_ctx`` Depends
helper so auth + session cookie plumbing is consistent across the
Teams BFF.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import WatchdogDecisionBody

router = APIRouter()

# Paperclip ids are UUIDs or LB-12-style identifiers (`_` admitted for test ids). Pinning keeps a
# percent-decoded `?`/`&`/`/` in the path segment from rewriting the fixed
# upstream path (e.g. `<id>%3Fmode=cancel%26` smuggling a query string).
RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]
_ctx = _agents._ctx

# Default tail size for the live-run transcript read. Kept small so the
# Dashboard cards stay glanceable (a recent-activity snippet, not a full
# scrollback). Upstream's own clamp ceiling is 1 MiB; we cap our public
# param well below that so a card poll can never pull a megabyte.
RUN_LOG_DEFAULT_LIMIT_BYTES = 65_536
RUN_LOG_MAX_LIMIT_BYTES = 262_144

# Interpolated into a fixed upstream path, so it may not carry "/", "." or "?".
AgentId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


@router.get("/agents/{agent_id}/runs")
async def list_agent_runs(
    agent_id: AgentId,
    limit: int = Query(200, ge=1, le=1000),
    ctx: TeamsContext = Depends(_ctx),
):
    """One agent's heartbeat runs, newest first — the ported stock agent
    Dashboard/Runs tabs' ``heartbeatsApi.list(companyId, agentId)``.

    The BFF builds upstream ``GET /api/companies/{own}/heartbeat-runs?agentId=``
    itself: the company is always the caller's workspace, and the agent must be
    in it (404 otherwise) — a Paperclip user can belong to several companies, so
    the member session alone does not pin the agent to THIS workspace. Stock
    sends no limit (unbounded); ours defaults to upstream's own 200.
    """
    agent = await _agents._admin().get_agent(agent_id=agent_id, session_cookie=ctx.session_cookie)
    if not isinstance(agent, dict) or agent.get("companyId") != ctx.company_id or not agent.get("id"):
        raise HTTPException(status_code=404, detail="Agent not found")
    rows = await _agents._admin().list_company_heartbeat_runs(
        session_cookie=ctx.session_cookie,
        company_id=ctx.company_id,
        agent_id=agent["id"],
        limit=limit,
    )
    return envelope_list(rows, "runs")


async def _require_company_run(run_id: str, ctx: TeamsContext) -> dict:
    """404 unless heartbeat run ``run_id`` belongs to the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company, so the session alone does not pin a
    run to THIS workspace. Same fence as agents' ``_company_agent``.
    """
    run = await _agents._admin().get_heartbeat_run(run_id=run_id, session_cookie=ctx.session_cookie)
    if not isinstance(run, dict) or run.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _company_run(run_id: RunId, ctx: TeamsContext = Depends(_ctx)) -> str:
    await _require_company_run(run_id, ctx)
    return run_id


CompanyRunId = Annotated[str, Depends(_company_run)]


@router.get("/heartbeat-runs/{run_id}")
async def get_run(run_id: RunId, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single heartbeat-run by id.

    Maps to upstream ``GET /api/heartbeat-runs/{runId}``. Distinct from
    the legacy ``GET /teams/runs/{id}`` (in ``agents.py``) which targets
    upstream's generic ``/api/runs/{id}``.
    """
    return await _require_company_run(run_id, ctx)


@router.get("/heartbeat-runs/{run_id}/log")
async def get_run_log(
    run_id: CompanyRunId,
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
    run_id: CompanyRunId,
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
async def cancel_run(run_id: RunId, ctx: TeamsContext = Depends(_ctx)):
    """Cancel a queued/running heartbeat run, and stop its Hermes turn.

    Maps to upstream ``POST /api/heartbeat-runs/{runId}/cancel``. Stopping a
    run never *starts* spend, so no trial-runway gate here (unlike wakeup).
    Paperclip's cancel never reaches the hermes_gateway adapter, whose only stop
    is its own timeout (fork ``gateway/server/execute.ts``), so the turn kept
    running on the runtime after a cancel (dev 2026-10-16): stop it here too.
    """
    run = await _require_company_run(run_id, ctx)
    cancelled = await _agents._admin().cancel_heartbeat_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )
    from routers.messaging import stop_paperclip_hermes_turn

    await stop_paperclip_hermes_turn(ctx.owner_id, run)
    return cancelled


@router.post("/heartbeat-runs/{run_id}/watchdog-decisions")
async def record_watchdog_decision(
    run_id: CompanyRunId,
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
