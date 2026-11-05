"""The runtime's callbacks into task orchestration (wave-2 O Task 3; contracts §9, C13).

Auth: the owner's hook token (owner-scoped: every runtime of the workspace holds it) AND the attempt's own
`runSecret` (minted by `begin_attempt`, sent to the bridge in the run identity), so a runtime can only report
for the attempt it runs. 404 while the workspace's switch is off.

- `POST /api/v1/internal/runtime/{owner}/runs/{runId}/events`: the bridge's heartbeat (every 2 min) and
  terminal status for a v1 run. Heartbeats heartbeat the async `worker_turn` and record `recentTools`;
  the terminal event stores output and usage on the Attempt and completes the Activity once.
- `POST /api/v1/internal/runtime/{owner}/inputs`: a background job or delegation finished inside a task's
  session; recorded as a Trigger and signalled to the task (dedupe on `deliveryId`).
- `POST /api/v1/internal/runtime/{owner}/notes`: a Worker's `task_note`, posted in its own task's room
  (contracts-v2 §6: the only place a Worker posts), by the current live attempt only.

Lane Q appends its effects and questions routes to this router.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from temporalio.client import AsyncActivityCancelledError
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import effects_repo, tasks_repo
from core.services import questions, task_asks, task_conversation
from core.services.messaging import ATTACHMENT_MAX_COUNT, UploadedAttachment, human_display_name
from core.services.container_auth import _require_hook_token
from core.services.super_agent_flags import flags_for
from core.temporal import activities as acts
from core.temporal import client as temporal
from core.temporal.types import RunEvent, RunStatusName, RunUsage, TaskInputRef, WorkerTurnOut

router = APIRouter()
logger = logging.getLogger(__name__)

_LIVE = frozenset({"dispatched", "running"})
_CLEAN = acts._CLEAN  # terminal statuses that are not failures


_TOOLS_KEPT = 20


def _tool_line(t: dict) -> dict:
    """One recentTools entry, bounded (DynamoDB items cap at 400 KB): name, args hash, outcome only."""
    return {
        "name": str(t.get("name") or "")[:64],
        "argsHash": str(t.get("argsHash") or "")[:32],
        "ok": bool(t.get("ok", True)),
        "errorClass": (str(t["errorClass"])[:64] if t.get("errorClass") else None),
    }


def _secret_ok(att: dict | None, presented: str | None) -> bool:
    expected = (att or {}).get("runSecret") or ""
    return bool(expected and presented) and hmac.compare_digest(presented.encode(), expected.encode())


class RunEventBody(BaseModel):
    task_id: str = Field(alias="taskId")
    attempt: int = Field(ge=1)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    kind: Literal["heartbeat", "terminal"]
    # `waiting_approval`: the Worker's turn ended because an external effect needs a person (Lane Q, contracts §8).
    status: RunStatusName  # one source with the workflow's RunEvent (a mismatch dropped run_event signals, #4824)
    output: str | None = Field(None, max_length=65536)
    error: str | None = Field(None, max_length=128)
    usage: dict[str, Any] | None = None
    recent_tools: list[dict[str, Any]] = Field(default_factory=list, alias="recentTools", max_length=50)
    # Files the Worker's result named (`MEDIA:` lines), already PUT by the task VM to the TASK ROOM's
    # attachment keys; the result post adopts them (an older bridge sends none).
    attachments: list[UploadedAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)


class InputBody(BaseModel):
    task_id: str = Field(alias="taskId")
    session_key: str = Field(alias="sessionKey")
    kind: Literal["job_completed", "delegation_completed"]
    job_id: str = Field(alias="jobId", max_length=256)
    text: str = Field(max_length=32 * 1024)
    delivery_id: str = Field(alias="deliveryId", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)


def _not_found(exc: RPCError) -> bool:
    return exc.status == RPCStatusCode.NOT_FOUND


async def _gate(owner_id: str, request: Request) -> None:
    """Owner-scoped hook token, then the workspace's switch (off ⇒ 404: the routes do not exist)."""
    await _require_hook_token(owner_id, request)
    if not await flags_for(owner_id):
        raise HTTPException(status_code=404, detail="not_found")


@router.post("/{owner_id}/runs/{run_id}/events")
async def run_event(owner_id: str, run_id: str, body: RunEventBody, request: Request) -> dict:
    await _gate(owner_id, request)
    att = await tasks_repo.get_attempt(owner_id, body.task_id, body.attempt)
    # The owner's hook token is shared by all its runtimes: the attempt's own secret binds the event to it.
    if att is None or att.get("runId") != run_id or not _secret_ok(att, body.run_secret):
        raise HTTPException(status_code=403, detail="run_not_in_workspace")
    token = att.get("taskToken")
    if not token:
        raise HTTPException(status_code=409, detail="attempt_not_started")  # the bridge retries
    handle = temporal.get_client().get_async_activity_handle(task_token=base64.b64decode(token))

    if body.kind == "heartbeat":
        # One line per attempt, on the first heartbeat with a tool a person would recognise: an internal
        # tool (reading its own skills, notes) posts nothing, and the line waits for a real one.
        label = None if att.get("progressPosted") else progress_label(body.recent_tools)
        if att["status"] in _LIVE:
            await tasks_repo.update_attempt(
                owner_id,
                body.task_id,
                body.attempt,
                {
                    "lastHeartbeatAt": utc_now_iso(),
                    "recentTools": [_tool_line(t) for t in body.recent_tools[-_TOOLS_KEPT:]],
                    **({"progressPosted": True} if label else {}),
                },
                allowed_status=_LIVE,
                bump=False,  # every 2 min: no workspace-wide task.changed (a live view polls the trace)
            )
        if label:
            await _progress_line(owner_id, body.task_id, body.attempt, label)
        try:
            await handle.heartbeat(body.status)
        except AsyncActivityCancelledError:
            await handle.report_cancellation()  # Stop was pressed: the turn ends cancelled
        except RPCError as exc:
            if not _not_found(exc):
                raise
        return {"ok": True}

    fields: dict[str, Any] = {"terminalStatus": body.status, "output": body.output or ""}
    if body.attachments:
        fields["attachments"] = [a.model_dump(mode="json", by_alias=True) for a in body.attachments]
    if body.usage:
        fields["usage"] = acts._usage(body.usage)
    if not await tasks_repo.claim_attempt_terminal(owner_id, body.task_id, body.attempt, fields):
        return {"ok": True, "replayed": True}
    outcome = acts._TERMINAL[body.status] if body.status in acts._TERMINAL else "failed"
    try:
        await handle.complete(WorkerTurnOut(outcome, None if outcome in _CLEAN else "RunFailed"))
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


#: Tools a person would not recognise as work: the Worker reading its own skills, notes or plan.
_INTERNAL_TOOLS = frozenset(
    {"skill_view", "skills_list", "skill_manage", "memory", "todo", "session_search", "clarify"}
)
#: Tool-id prefix -> what the progress line says (first match wins). ponytail: a short fixed list; an
#: unknown tool reads "Working on it"; extend here when a tool shows up raw.
_TOOL_WORDS = (
    ("web_", "Searching the web"),
    ("browser_", "Using the browser"),
    ("terminal", "Running a command"),
    ("process", "Running a command"),
    ("execute_code", "Running code"),
    ("read_file", "Reading files"),
    ("search_files", "Reading files"),
    ("write_file", "Writing a file"),
    ("patch", "Writing a file"),
    ("image_generate", "Making an image"),
    ("vision_", "Looking at an image"),
    ("delegate_task", "Handing part of the work to a helper"),
    ("mcp_", "Using a connected app"),
    ("send_message", "Sending a message"),
)


def progress_label(tools: list[dict]) -> str | None:
    """Plain words for the first non-internal tool of a heartbeat; None when there is none yet."""
    for tool in tools:
        name = str(tool.get("name") or tool.get("tool") or "")
        if not name or name in _INTERNAL_TOOLS:
            continue
        return next((words for prefix, words in _TOOL_WORDS if name.startswith(prefix)), "Working on it")
    return None


async def _progress_line(ws: str, task_id: str, n: int, label: str) -> None:
    """§14.11 progress poster: one line per attempt in the task room (`progress_label`). Every heartbeat
    updates the Attempt's `recentTools` (the trace and live views read it)."""
    try:
        await task_conversation.post_as_lightbulb(
            ws,
            ts.task_room_id(task_id),
            text=f"{label}…",
            event_id=f"task-progress:{task_id}:{n}",
            progress=True,
        )
    except Exception:
        logger.warning("progress line failed", exc_info=True)


@router.post("/{owner_id}/inputs", status_code=202)
async def task_input(owner_id: str, body: InputBody, request: Request, response: Response) -> dict:
    await _gate(owner_id, request)
    tree = await tasks_repo.get_task_tree(owner_id, body.task_id)
    task = tree["task"]
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    last = max(tree["attempts"], key=lambda a: a["n"], default=None)
    key_ok = last is not None and body.session_key == ts.worker_session_key(last["agentId"], body.task_id)
    if not key_ok or not _secret_ok(last, body.run_secret):
        raise HTTPException(status_code=403, detail="not_this_tasks_runtime")
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


# --- effects (Lane Q, wave2-Q Task 5; contracts-v2 §8) ---------------------------------------------------
# One route for every external, irreversible effect a Worker attempts: the bridge's F1 callback asks before the
# tool runs. Only answering a Question approves; this route only reads the answers (`questions.gate`).

_OPERANDS_MAX = 16 * 1024


def operands_hash(kind: str, operands: dict) -> str:
    """The bridge's canonical hash (bridge/effects.py `operands_hash`): sha256 of `{kind, **operands}` as sorted,
    compact JSON. Recomputed here so a runtime cannot pair one call's operands with another's hash."""
    canonical = json.dumps({"kind": kind, **operands}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class EffectBody(BaseModel):
    task_id: str = Field(alias="taskId")
    attempt: int = Field(ge=1)
    run_id: str = Field(alias="runId")
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    tool_call_id: str = Field(alias="toolCallId", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    kind: Literal["mail.send", "text.send", "app.publish", "pr.merge", "purchase", "provision", "a2a.send"]
    tool: str = Field(max_length=64)
    operands: dict[str, Any] = Field(default_factory=dict)
    operands_hash: str = Field(alias="operandsHash", pattern=r"^[0-9a-f]{64}$")
    executor_ok: bool = Field(False, alias="executorOk")
    idem_key: str = Field(alias="idemKey", max_length=256)

    @field_validator("operands")
    @classmethod
    def _bounded(cls, v: dict) -> dict:
        if len(json.dumps(v, default=str)) > _OPERANDS_MAX:
            raise ValueError("operands over 16 KB")
        return v


class EffectDoneBody(BaseModel):
    disposition: Literal["done", "unknown"]
    result_ref: str | None = Field(None, alias="resultRef", max_length=512)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    run_id: str = Field(alias="runId")


async def _attempt_of(owner_id: str, task_id: str, n: int, run_id: str, run_secret: str) -> dict:
    """The attempt this runtime speaks for, or 403 (wrong run or secret)."""
    att = await tasks_repo.get_attempt(owner_id, task_id, n)
    if att is None or att.get("runId") != run_id or not _secret_ok(att, run_secret):
        raise HTTPException(status_code=403, detail="run_not_in_workspace")
    return att


@router.post("/{owner_id}/effects")
async def effect_check(owner_id: str, body: EffectBody, request: Request) -> dict:
    await _gate(owner_id, request)
    att = await _attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
    task = await tasks_repo.get_task(owner_id, body.task_id)
    # A later attempt never reuses an earlier one's approval; a stopped or ended attempt never gets a new one.
    if task is None or task.get("attemptCount") != body.attempt or att.get("status") not in _LIVE:
        raise HTTPException(status_code=409, detail="stale_run")
    if body.idem_key != f"{body.task_id}:{body.attempt}:{body.tool_call_id}":
        raise HTTPException(status_code=400, detail="invalid_idem_key")
    if body.operands_hash != operands_hash(body.kind, body.operands):
        raise HTTPException(status_code=400, detail="operands_hash_mismatch")
    effect = {
        "idemKey": body.idem_key,
        "taskId": body.task_id,
        "attempt": body.attempt,
        "kind": body.kind,
        "tool": body.tool,
        "operands": body.operands,
        "operandsHash": body.operands_hash,
        "executorOk": body.executor_ok,
        "state": "proposed",
    }
    for _ in range(2):  # one re-read when the approval was used under us (Landing or a parallel call won)
        res = questions.gate(effect, await effects_repo.effects_for(owner_id, body.task_id, body.operands_hash))
        if res.decision != "proceed":
            break
        if res.approved_idem_key == body.idem_key:
            # The caller's own earlier call is the approved row: Landing (or a fresh call) runs it, never a re-run of
            # the call that was approved.
            return {
                "decision": "refuse",
                "message": "This call was approved and is being landed by Lightbulb; do not run it yourself.",
            }
        if await effects_repo.use_approval(owner_id, res.approved_idem_key, body.operands_hash, new_effect=effect):
            return {"decision": "proceed"}
    if res.decision == "proceed":  # the second read still says proceed but the row is gone: unknown outcome
        return {"decision": "refuse", "message": "A matching call's approval was just used; check before retrying."}
    if res.decision == "replay":
        return {"decision": "replay", "result": res.result_ref}
    if res.decision == "refuse":
        return {"decision": "refuse", "message": res.message}
    if res.decision == "denied":
        return {"decision": "denied", "questionId": res.qid}
    # needs_approval: the proposed row is the draft (Output) the person reviews; the workflow opens the Question
    # once the turn ends `waiting_approval`.
    row, _ = await effects_repo.put_effect(
        owner_id, {**effect, "questionId": res.qid, "output": questions.output_for(effect)}
    )
    return {"decision": "needs_approval", "questionId": row.get("questionId") or res.qid}


@router.post("/{owner_id}/effects/{idem_key}/done")
async def effect_done(owner_id: str, idem_key: str, body: EffectDoneBody, request: Request) -> dict:
    """`intent → done | unknown`, the bridge's commit after the tool ran (contracts §7's one forward-only
    exception). Accepted after Stop too: a late commit still records what happened."""
    await _gate(owner_id, request)
    effect = await effects_repo.get_effect(owner_id, idem_key)
    if effect is None:
        raise HTTPException(status_code=404, detail="effect_not_found")
    await _attempt_of(owner_id, effect["taskId"], effect["attempt"], body.run_id, body.run_secret)
    try:
        row = await effects_repo.mark_effect_done(
            owner_id, idem_key, disposition=body.disposition, result_ref=body.result_ref
        )
    except tasks_repo.IllegalTransition as exc:
        raise HTTPException(status_code=409, detail={"code": "not_intent", "state": exc.current}) from exc
    return {"state": row["state"]}


# --- a Worker's questions (Lane Q, wave2-Q Task 7) ---------------------------------------------------------
# `ask_person` / `request_approval` on a task run. The text rides on `TRIG#ask:{qid}`; the `ask` update on
# TaskWorkflow carries ids only and opens the Question row (one open `question` per task: a new one supersedes).


class AskOption(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    label: str = Field(min_length=1, max_length=200)


class AskBody(BaseModel):
    task_id: str = Field(alias="taskId")
    run_id: str = Field(alias="runId")
    attempt: int = Field(ge=1)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    tool_call_id: str = Field(alias="toolCallId", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    kind: Literal["question", "approval"]
    # Context-free subkinds only (spec 05 §3.3): `publish` needs the app and `a2a_hold` governance's holders, which
    # a Worker's ask cannot carry, so they are refused here rather than failing after the trigger is written.
    subkind: Literal["question", "tool", "send", "spend", "purchase", "provider_consent", "review"]
    title: str = Field(min_length=1, max_length=200)
    text: str = Field("", max_length=500)
    options: list[AskOption] = Field(default_factory=list, max_length=4)
    blocking: bool = False


async def _current_task(owner_id: str, att: dict, task_id: str, n: int) -> dict:
    """The task, when `att` is its current live attempt; 409 otherwise (a later attempt never speaks for an earlier one)."""
    task = await tasks_repo.get_task(owner_id, task_id)
    if task is None or task.get("attemptCount") != n or att.get("status") not in _LIVE:
        raise HTTPException(status_code=409, detail="stale_run")
    return task


async def _question_view(q: dict) -> dict:
    names = [await human_display_name(u) for u in q.get("answerers") or []]
    return {"questionId": q["qid"], "state": q["state"], "waitingLabel": questions.waiting_label(names)}


@router.post("/{owner_id}/questions")
async def worker_ask(owner_id: str, body: AskBody, request: Request) -> dict:
    await _gate(owner_id, request)
    att = await _attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
    task = await _current_task(owner_id, att, body.task_id, body.attempt)
    qid = questions.qid_for(f"{body.task_id}:{body.run_id}:{body.tool_call_id}")
    try:
        opened = await task_asks.open_ask(
            owner_id,
            task,
            qid=qid,
            kind=body.kind,
            subkind=body.subkind,
            title=body.title,
            text=body.text,
            options=[o.model_dump() for o in body.options],
            blocking=body.blocking,
            attempt=body.attempt,
            run_id=body.run_id,
        )
    except task_asks.AskRefused as exc:
        status = 503 if exc.code in ("temporal_unavailable", "question_not_opened") else 409
        raise HTTPException(status_code=status, detail=exc.code) from exc
    return await _question_view(opened)


NOTE_CAP = 20  # notes per attempt


class NoteBody(BaseModel):
    task_id: str = Field(alias="taskId")
    run_id: str = Field(alias="runId")
    attempt: int = Field(ge=1)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)


@router.post("/{owner_id}/notes")
async def task_note(owner_id: str, body: NoteBody, request: Request) -> dict:
    """A Worker's progress note in its task's room, as Lightbulb · specialist. The same text twice in one attempt
    posts once (a tool retry)."""
    await _gate(owner_id, request)
    att = await _attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
    task = await _current_task(owner_id, att, body.task_id, body.attempt)
    if not await tasks_repo.count_note(owner_id, body.task_id, body.attempt, NOTE_CAP):
        raise HTTPException(status_code=429, detail="note_limit")
    room_id = await task_conversation.ensure_task_room(owner_id, task)
    digest = hashlib.sha256(body.text.encode()).hexdigest()[:16]
    message_id = await task_conversation.post_as_lightbulb(
        owner_id,
        room_id,
        text=body.text,
        event_id=f"task-note:{body.task_id}:{body.attempt}:{digest}",
        specialist=task.get("specialist"),
        kind="note",
        progress=True,  # never a mention or notification, never room memory
    )
    return {"messageId": message_id}
