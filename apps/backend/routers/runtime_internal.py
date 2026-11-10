"""The runtime's callbacks into task orchestration (wave-2 O Task 3; contracts §9, C13).

Auth: the owner's hook token (owner-scoped: every runtime of the workspace holds it) AND the attempt's own
`runSecret` (minted by `begin_attempt`, sent to the bridge in the run identity), so a runtime can only report
for the attempt it runs.

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

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from temporalio.client import AsyncActivityCancelledError
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import billing_repo, effects_repo, questions_repo, tasks_repo
from core.services import (
    purchases,
    questions,
    runtime_runs,
    runtime_transport,
    task_asks,
    task_conversation,
    tasks_access,
)
from core.services.messaging import ATTACHMENT_MAX_COUNT, UploadedAttachment, human_display_name
from core.services.container_auth import _require_hook_token
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
    # The session's scoped AWS credentials end within 15 min (bridge.aws_session_due): a run sends
    # no invoke while it runs, so this heartbeat is the one chance to hand it a fresh set.
    credentials_due: bool = Field(False, alias="credentialsDue")
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


_REFRESHES: set[asyncio.Task] = set()


def _refresh_credentials(owner_id: str, task_id: str, att: dict) -> None:
    """Poll the run once, in the background: a poll is an invoke on the run's own session, and every
    invoke carries the backend's current scoped set (``runtime_transport._session_credentials_header``
    re-mints 15 min before expiry; the bridge keeps the later-expiring set). In the background because
    the poll may outlast the bridge's 20 s event timeout."""

    async def _poll() -> None:
        try:
            await runtime_runs.get_run(owner_id, runtime_runs.RunIdentity.for_attempt(owner_id, task_id, att))
        except Exception:  # noqa: BLE001 -- the next heartbeat asks again
            logger.info("credentials refresh poll failed task=%s", task_id, exc_info=True)

    task = asyncio.create_task(_poll())
    _REFRESHES.add(task)
    task.add_done_callback(_REFRESHES.discard)


def _not_found(exc: RPCError) -> bool:
    return exc.status == RPCStatusCode.NOT_FOUND


async def _gate(owner_id: str, request: Request) -> None:
    """Owner-scoped hook token."""
    await _require_hook_token(owner_id, request)


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
        identity = runtime_runs.RunIdentity.for_attempt(owner_id, body.task_id, att)
        await run_in_thread(
            runtime_transport.note_alive, owner_id, body.task_id if identity.session_kind == "task" else None
        )
        if body.credentials_due:
            _refresh_credentials(owner_id, body.task_id, att)
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
        task = await tasks_repo.get_task(ws, task_id) or {}
        await task_conversation.post_as_lightbulb(
            ws,
            ts.task_room_id(task_id),
            text=f"{label}…",
            event_id=f"task-progress:{task_id}:{n}",
            specialist=task.get("specialist"),  # the specialist doing the work posts it, like its result (N-F6)
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


async def _mode_allows(ws: str, kind: str, operands: dict) -> str | None:
    """`mode:<mode>` when the workspace's approval mode lets this action go ahead without a card, else None. Read
    here on the server (the VM never sees it). Members and limits are read only when the Auto list needs them; a
    Clerk error fails closed (asks)."""
    if kind == "purchase" and await purchases.needs_person(ws, str(operands.get("url") or "")):
        return None  # the Question is the owner's yes: money_moves on, or a merchant not allowlisted
    mode = (await billing_repo.get_capability_policy(ws))["approval_mode"]
    members: set[str] = set()
    if mode == "auto" and kind in ("mail.send", "text.send"):
        try:
            members = await questions.member_emails(ws)
        except Exception:  # noqa: BLE001 -- an unreadable roster is nobody: the card asks
            logger.warning("approval mode: members unreadable ws=%s", ws, exc_info=True)
    limits = await billing_repo.get_purchase_limits(ws) if mode == "auto" and kind == "purchase" else None
    return None if questions.asks(mode, kind, operands, members=members, limits=limits) else f"mode:{mode}"


#: An identical effect that went ahead in the same task this recently is replayed, never run twice (gate run 9).
_DUPLICATE_WINDOW = timedelta(minutes=10)


def _recent_since() -> str:
    return (datetime.now(timezone.utc) - _DUPLICATE_WINDOW).isoformat()


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
    # Questions a Close cancelled: their proposed calls are never reused after a Reopen (dev walk 7).
    closed = {q["qid"] for q in await questions_repo.list_for_task(owner_id, body.task_id) if q["state"] == "cancelled"}
    for _ in range(2):  # one re-read when the approval was used under us (Landing or a parallel call won)
        prior = await effects_repo.effects_for(owner_id, body.task_id, body.operands_hash)
        res = questions.gate(effect, prior, closed, recent_since=_recent_since())
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
        return {"decision": "replay", "result": res.message or res.result_ref}
    if res.decision == "refuse":
        return {"decision": "refuse", "message": res.message}
    if res.decision == "denied":
        return {"decision": "denied", "questionId": res.qid}
    # needs_approval, unless a standing rule or the workspace's approval mode lets it through without a card. The
    # intent row it goes ahead on is the log (and what a backend door and the bridge's `done` look for).
    rule = await questions.rule_effect(owner_id, task.get("specialist"), body.kind, body.operands)
    if rule and rule[0] == "deny":
        return {"decision": "denied", "questionId": None}
    if await tasks_repo.from_contact(owner_id, task):
        allowed_by = None  # a phone caller's request: no rule or mode spends, mails or changes things for them
    elif task.get("confirmAll"):
        allowed_by = None  # started from a meeting: whoever spoke, the inviter confirms every spend, send or publish
    else:
        allowed_by = f"rule:{rule[1]}" if rule else await _mode_allows(owner_id, body.kind, body.operands)
    if allowed_by:
        row, created = await effects_repo.put_effect(owner_id, {**effect, "state": "intent", "allowedBy": allowed_by})
        if created:
            return {"decision": "proceed"}
        # this very call was already asked about (its row is proposed): it waits for that answer
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
    await tasks_access.record_effect_output(owner_id, row)  # also on a retried done: the write is idempotent
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


class FrontDeskBody(BaseModel):
    task_id: str = Field(alias="taskId")
    run_id: str = Field(alias="runId")
    attempt: int = Field(ge=1)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)
    action: Literal["availability", "book", "my_bookings", "move", "cancel", "message"]
    date: str = ""
    service: str = ""
    start: str = ""
    new_start: str | None = None
    name: str = ""
    text: str = ""
    callback_number: str = ""


@router.post("/{owner_id}/front-desk")
async def front_desk(owner_id: str, body: FrontDeskBody, request: Request) -> dict:
    """A phone caller's thread acts for that caller (governance P2, the caller case): the call comes from the task's
    own trigger, never the model, and each action is the phone's own route, which keeps a caller to their own
    bookings and honours the Agent page's allow switches. 403 for any task that is not a caller's."""
    from pydantic import ValidationError

    from core.services import business_profile
    from routers import voice_phone as vp

    await _gate(owner_id, request)
    att = await _attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
    task = await _current_task(owner_id, att, body.task_id, body.attempt)
    trig = await tasks_repo.get_trigger(owner_id, task["triggerId"]) if task.get("triggerId") else None
    call_id = ((trig or {}).get("ref") or {}).get("callId")
    meta = await vp.messaging_repo.get_room(call_id) if call_id else None
    if (
        not await tasks_repo.from_contact(owner_id, task)
        or (meta or {}).get("kind") != "call"
        or meta.get("ownerId") != owner_id
    ):
        raise HTTPException(status_code=403, detail="not_a_caller_thread")
    try:
        if body.action == "availability":
            return await vp.availability(call_id, vp.DateIn(date=body.date, service=body.service))
        if body.action == "my_bookings":
            return await vp.my_bookings(call_id)
        if body.action == "message":
            return await vp.take_message(
                call_id, vp.MessageIn(name=body.name, text=body.text, callbackNumber=body.callback_number)
            )
        try:
            if body.action == "book":
                return await vp.book(call_id, vp.BookIn(start=body.start, service=body.service, name=body.name))
            new = body.new_start if body.action == "move" else None
            return await vp.change_booking(call_id, vp.ChangeIn(start=body.start, newStart=new))
        finally:  # the caller's batch and thread see the change at once, not after the 60 s cache
            business_profile.forget(owner_id)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


class MeetingContextBody(BaseModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=128)
    trigger_id: str = Field(alias="triggerId", min_length=1, max_length=500)
    section: Literal["transcript", "tasks"]
    cursor: str | None = Field(default=None, max_length=512)


@router.post("/{owner_id}/meeting-context")
async def meeting_context(owner_id: str, body: MeetingContextBody, request: Request) -> dict:
    from core.services import meeting_context as source

    await _gate(owner_id, request)
    try:
        return await source.context(
            owner_id,
            body.run_id,
            body.trigger_id,
            body.section,
            body.cursor,
            grant=request.headers.get(source.GRANT_HEADER),
        )
    except source.NotAuthorized as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


class MeetingChatBody(BaseModel):
    trigger_ids: list[str] = Field(alias="triggerIds", min_length=1, max_length=10)
    text: str = Field(min_length=1, max_length=500)


@router.post("/{owner_id}/meeting-chat")
async def meeting_chat(owner_id: str, body: MeetingChatBody, request: Request) -> dict:
    from core.services import meeting_chat as delivery

    await _gate(owner_id, request)
    try:
        return await delivery.send(owner_id, body.trigger_ids, body.text)
    except delivery.NotCurrentMeeting as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except delivery.meeting_invites.MeetingBusy as exc:
        raise HTTPException(status_code=503, detail="meeting_busy") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_arguments") from exc


class MeetingStopTranscribingBody(BaseModel):
    trigger_ids: list[str] = Field(alias="triggerIds", min_length=1, max_length=10)
    participant_id: str = Field(default="", alias="participantId", max_length=128)


@router.post("/{owner_id}/meeting-stop-transcribing")
async def meeting_stop_transcribing(owner_id: str, body: MeetingStopTranscribingBody, request: Request) -> dict:
    from core.services import meeting_chat as delivery

    await _gate(owner_id, request)
    try:
        return await delivery.stop_transcribing(owner_id, body.trigger_ids, body.participant_id)
    except delivery.NotCurrentMeeting as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except delivery.meeting_invites.MeetingBusy as exc:
        raise HTTPException(status_code=503, detail="meeting_busy") from exc


class TransferCallBody(BaseModel):
    trigger_ids: list[str] = Field(alias="triggerIds", min_length=1, max_length=10)
    text: str = Field(min_length=1, max_length=500)


@router.post("/{owner_id}/transfer-call")
async def transfer_call(owner_id: str, body: TransferCallBody, request: Request) -> dict:
    """The Router's transfer_call (a phone caller asked for a person): one Lightbulb line in the call room marked
    `action: "transfer"`, the trigger's answer. The phone worker sees it on /events and runs /transfer and the SIP
    transfer itself (huddle-agent phone.py `hand_off`); nothing is transferred here. Only a caller's triggers, all
    from one call, in that call's own room while the line is live."""
    from routers import voice_phone as vp

    await _gate(owner_id, request)
    triggers = [await tasks_repo.get_trigger(owner_id, t) for t in body.trigger_ids]
    refs = [(t or {}).get("ref") or {} for t in triggers]
    calls = {r.get("callId") if r.get("origin") == "phone_contact" else None for r in refs}
    call_id = next(iter(calls)) if len(calls) == 1 else None
    if not call_id or any(r.get("roomId") != call_id for r in refs):
        raise HTTPException(status_code=403, detail="not_a_caller")
    meta = await vp.messaging_repo.get_room(call_id) if call_id else None
    if not meta or meta.get("kind") != "call" or meta.get("ownerId") != owner_id:
        raise HTTPException(status_code=403, detail="not_a_caller")
    caller = (meta.get("call") or {}).get("caller", "")
    if vp._line_state(await vp.agent_config(owner_id), caller) != "live":
        raise HTTPException(status_code=403, detail="not_allowed")  # a paused line only takes messages
    message_id = await task_conversation.post_as_lightbulb(
        owner_id,
        call_id,
        text=vp.redact_cards(body.text),
        event_id=f"call-transfer:{body.trigger_ids[0]}",
        attrs={"action": "transfer"},
        remember=False,
    )
    for t in triggers:  # the line is this ask's answer: the Router posts no second line for it
        await tasks_repo.set_trigger_routed_to(
            owner_id, t["triggerId"], {**(t.get("routedTo") or {}), "transfer": message_id}
        )
    return {"messageId": message_id}


NOTE_CAP = 20  # notes per attempt


class RunRef(BaseModel):
    task_id: str = Field(alias="taskId")
    run_id: str = Field(alias="runId")
    attempt: int = Field(ge=1)
    run_secret: str = Field(alias="runSecret", min_length=1, max_length=128)


class NoteBody(RunRef):
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


@router.post("/{owner_id}/business-facts")
async def business_facts(owner_id: str, body: RunRef, request: Request) -> dict:
    """A Worker's `business_facts`: its own workspace's Agent page (services, prices, hours, the Knowledge answers),
    without the abbreviated profile's character cap; later saved answers must remain readable.
    Read-only; the workspace is the path's, proven by the hook token and the attempt's secret."""
    from core.services import business_profile

    await _gate(owner_id, request)
    await _attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
    return {"facts": await business_profile.facts_text(owner_id)}
