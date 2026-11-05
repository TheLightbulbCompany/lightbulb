"""The runtime's callbacks into task orchestration (wave-2 O Task 3; contracts §9, C13). Hook-token authed.

- `POST /api/v1/internal/runtime/{owner}/runs/{runId}/events`: the bridge's heartbeat (every 2 min) and
  terminal status for a v1 run. Heartbeats heartbeat the async `worker_turn` and record `recentTools`;
  the terminal event stores output and usage on the Attempt and completes the Activity once.
- `POST /api/v1/internal/runtime/{owner}/inputs`: a background job or delegation finished inside a task's
  session; recorded as a Trigger and signalled to the task (dedupe on `deliveryId`).

Lane Q appends its effects and questions routes to this router.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from temporalio.client import AsyncActivityCancelledError
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import tasks_repo
from core.services import task_conversation
from core.services.container_auth import _require_hook_token
from core.temporal import activities as acts
from core.temporal import client as temporal
from core.temporal.types import RunEvent, RunUsage, TaskInputRef, WorkerTurnOut

router = APIRouter()
logger = logging.getLogger(__name__)

_LIVE = frozenset({"dispatched", "running"})


class RunEventBody(BaseModel):
    task_id: str = Field(alias="taskId")
    attempt: int = Field(ge=1)
    kind: Literal["heartbeat", "terminal"]
    status: Literal["queued", "running", "stopping", "completed", "failed", "cancelled"]
    output: str | None = Field(None, max_length=65536)
    error: str | None = Field(None, max_length=128)
    usage: dict[str, Any] | None = None
    recent_tools: list[dict[str, Any]] = Field(default_factory=list, alias="recentTools", max_length=50)


class InputBody(BaseModel):
    task_id: str = Field(alias="taskId")
    session_key: str = Field(alias="sessionKey")
    kind: Literal["job_completed", "delegation_completed"]
    job_id: str = Field(alias="jobId", max_length=256)
    text: str = Field(max_length=32 * 1024)
    delivery_id: str = Field(alias="deliveryId", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


def _not_found(exc: RPCError) -> bool:
    return exc.status == RPCStatusCode.NOT_FOUND


@router.post("/{owner_id}/runs/{run_id}/events")
async def run_event(owner_id: str, run_id: str, body: RunEventBody, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    att = await tasks_repo.get_attempt(owner_id, body.task_id, body.attempt)
    if att is None or att.get("runId") != run_id:
        raise HTTPException(status_code=403, detail="run_not_in_workspace")
    token = att.get("taskToken")
    if not token:
        raise HTTPException(status_code=409, detail="attempt_not_started")  # the bridge retries
    handle = temporal.get_client().get_async_activity_handle(task_token=base64.b64decode(token))

    if body.kind == "heartbeat":
        first = not att.get("recentTools") and body.recent_tools
        if att["status"] in _LIVE:
            await tasks_repo.update_attempt(
                owner_id,
                body.task_id,
                body.attempt,
                {"lastHeartbeatAt": utc_now_iso(), "recentTools": body.recent_tools},
                allowed_status=_LIVE,
            )
        if first:
            await _progress_line(owner_id, body.task_id, body.attempt, body.recent_tools)
        try:
            await handle.heartbeat(body.status)
        except AsyncActivityCancelledError:
            await handle.report_cancellation()  # Stop was pressed: the turn ends cancelled
        except RPCError as exc:
            if not _not_found(exc):
                raise
        return {"ok": True}

    fields: dict[str, Any] = {"terminalStatus": body.status, "output": body.output or ""}
    if body.usage:
        fields["usage"] = acts._usage(body.usage)
    if not await tasks_repo.claim_attempt_terminal(owner_id, body.task_id, body.attempt, fields):
        return {"ok": True, "replayed": True}
    outcome = acts._TERMINAL[body.status] if body.status in acts._TERMINAL else "failed"
    try:
        await handle.complete(WorkerTurnOut(outcome, None if outcome == "completed" else "RunFailed"))
    except RPCError as exc:
        if not _not_found(exc):  # not found = the turn already ended (stopped, timed out): nothing to complete
            await tasks_repo.unclaim_attempt_terminal(owner_id, body.task_id, body.attempt)
            raise HTTPException(status_code=503, detail="temporal_unavailable") from exc
    try:
        usage = fields.get("usage") or {}
        await temporal.task_handle(owner_id, body.task_id).signal(
            "run_event",
            RunEvent(
                run_id,
                "terminal",
                body.status,
                RunUsage(usage.get("inputTokens", 0), usage.get("outputTokens", 0), usage.get("costUsd", 0.0)),
            ),
        )
    except RPCError:
        logger.warning("run_event signal failed", exc_info=True)  # completion already delivered the result
    return {"ok": True}


async def _progress_line(ws: str, task_id: str, n: int, tools: list[dict]) -> None:
    """§14.11 progress poster: one line per attempt in the task room, on the first heartbeat with tools.
    Later heartbeats update the Attempt's `recentTools` (the trace and live views read it)."""
    name = next((t.get("name") or t.get("tool") for t in tools if t.get("name") or t.get("tool")), "tools")
    try:
        await task_conversation.post_as_lightbulb(
            ws,
            ts.task_room_id(task_id),
            text=f"Working: {name}",
            event_id=f"task-progress:{task_id}:{n}",
            progress=True,
        )
    except Exception:
        logger.warning("progress line failed", exc_info=True)


@router.post("/{owner_id}/inputs", status_code=202)
async def task_input(owner_id: str, body: InputBody, request: Request, response: Response) -> dict:
    await _require_hook_token(owner_id, request)
    task = await tasks_repo.get_task(owner_id, body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    trigger_id = ts.trigger_id("job_result", body.delivery_id)
    _, created = await tasks_repo.put_trigger(
        owner_id,
        {
            "triggerId": trigger_id,
            "source": "job_result",
            "ref": {"text": body.text, "jobId": body.job_id, "kind": body.kind, "taskId": body.task_id},
            "admission": "respond",
            "lane": task.get("lane") or "shared",
            "receivedAt": utc_now_iso(),
        },
    )
    if not created:
        response.status_code = 200
    try:
        await temporal.signal_task_input(owner_id, body.task_id, TaskInputRef(trigger_id, body.kind))
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise HTTPException(status_code=503, detail="temporal_unavailable") from exc
    return {"triggerId": trigger_id, "replayed": not created}
