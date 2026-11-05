"""The Activities `TaskWorkflow` runs (wave-2 O Task 3; contracts-v2 §7, §9, §14.4, §14.8, §14.10).

Only these change task status, attempts and slots (the workflow is the single writer, §7). `worker_turn`
completes asynchronously: it starts the bridge run, stores its task token on the Attempt and returns
`raise_complete_async()`; the bridge's run events (`routers/runtime_internal.py`) heartbeat and complete it.
`end_attempt` does what `run.*` callbacks fed for room runs (§14.4): the result posted as Lightbulb, failure
push, invite ask, usage. Payloads are ids; text lives on rows.

Trigger `ref` shapes read here: chat `{roomId, messageId}` (text from the message), others `{text}`.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import logging
import time
from datetime import timedelta

from temporalio import activity

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo, tasks_repo
from core.services import chat_gate, messaging, push_service, runtime_runs, runtime_transport, task_conversation
from core.temporal.client import get_client
from core.temporal.types import AttemptRef, WorkerTurnIn, WorkerTurnOut

logger = logging.getLogger(__name__)

SHARED_VM_SLOTS = 3  # contracts §9 / C7
LEASE_S = int(timedelta(hours=7).total_seconds())  # outlives worker_turn's 6 h 30 m
BUSY_RETRY_S = 600
BUSY_PAUSE_S = 15
INPUT_MAX = 32 * 1024

WORKER_INSTRUCTIONS = (
    "You are working on one task for this workspace. Do the work, then answer with the result the person "
    "asked for. Post notes only in this task's own room; Lightbulb posts your result for you."
)

_STATUS_AFTER = {
    "completed": ("idle", None),
    "waiting_approval": ("waiting", "approval"),
    "cancelled": ("resolved", "stopped"),
}
_RUNNING = frozenset({"queued", "running", "stopping"})
_TERMINAL = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "waiting_approval": "waiting_approval",  # Lane Q: an external effect needs a person (contracts §8)
}
_CLEAN = frozenset({"completed", "waiting_approval"})


# --- helpers ---------------------------------------------------------------------------------------


async def _task(ws: str, task_id: str) -> dict:
    task = await tasks_repo.get_task(ws, task_id)
    if task is None:
        raise LookupError(f"task {task_id} not found")
    return task


async def _move(ws: str, task_id: str, to: str, reason: str | None = None, fields: dict | None = None) -> None:
    """Transition unless already there; walks queued first when the edge needs it."""
    task = await _task(ws, task_id)
    if task["status"] == to:
        return
    if to == "working" and task["status"] not in tasks_repo.TRANSITIONS["working"]:
        await tasks_repo.transition_task(ws, task_id, to="queued")
    await tasks_repo.transition_task(ws, task_id, to=to, reason=reason, fields=fields)


def _origin(tree: dict) -> tuple[str | None, str | None]:
    """(roomId, threadRoot) of the conversation the task came from: its Router-made subscription."""
    for sub in tree.get("subscriptions") or []:
        parts = sub.get("threadKey", "").split(":")
        if sub.get("createdBy") == "router" and parts[0] == "room" and len(parts) >= 2:
            return parts[1], (parts[2] if len(parts) > 2 else None)
    return (tree["task"].get("audience") or {}).get("roomId"), None


async def _input_text(ws: str, trigger_id: str) -> str:
    trig = await tasks_repo.get_trigger(ws, trigger_id) or {}
    ref = trig.get("ref") or {}
    if ref.get("text"):
        return str(ref["text"])[:INPUT_MAX]
    if ref.get("roomId") and ref.get("messageId"):
        msg = await messaging_repo.get_message_by_id(ref["roomId"], ref["messageId"]) or {}
        return (msg.get("editedText") or msg.get("text") or "")[:INPUT_MAX]
    return ""


def _identity(ws: str, task_id: str, att: dict) -> runtime_runs.RunIdentity:
    return runtime_runs.RunIdentity(
        att["runId"], ws, task_id, att["n"], att["agentId"], run_secret=att.get("runSecret")
    )


async def _agent_for(ws: str, task: dict) -> str:
    """The specialist's agent id when it is one of this workspace's agents, else Lightbulb's (the CEO)."""
    spec = task.get("specialist")
    if spec and spec in {a["id"] for a in await messaging.load_agent_roster(ws)}:
        return spec
    return await task_conversation.front_agent_id(ws)


# --- budget and slots ------------------------------------------------------------------------------


@activity.defn
async def check_budget(ws: str, task_id: str) -> bool:
    """§14.8: recorded usage vs `budgetUsd`, at start and between attempts only. Over ⇒ waiting/budget."""
    tree = await tasks_repo.get_task_tree(ws, task_id)
    budget = tree["task"].get("budgetUsd")
    if budget is None:
        return True
    spent = sum(float((a.get("usage") or {}).get("costUsd") or 0) for a in tree["attempts"])
    if spent < float(budget):
        return True
    await _move(ws, task_id, "waiting", "budget")
    return False


def _cap(session_kind: str) -> int:
    return (
        getattr(runtime_runs, "MAX_TASK_VMS_PER_WORKSPACE", SHARED_VM_SLOTS)
        if session_kind == "task"
        else (SHARED_VM_SLOTS)
    )


def _session_kind(ws: str) -> str:
    """V3's `session_kind_for`; the shared VM until it lands (README)."""
    fn = getattr(runtime_runs, "session_kind_for", None)
    return fn(ws) if fn else "workspace"


@activity.defn
async def acquire_slot(ws: str, task_id: str) -> bool:
    """Take a lease (cap per VM kind). Full ⇒ join the waiters and return False; `release_slot` signals."""
    cap = _cap(_session_kind(ws))
    for _ in range(8):
        row = await tasks_repo.get_slots(ws)
        now = int(time.time())
        # Expired leases are reclaimed: a lease outlives a terminated workflow, not forever.
        leases = {t: e for t, e in (row.get("leases") or {}).items() if int(e) > now}
        waiters = [w for w in row.get("waiters") or [] if w != task_id]
        granted = task_id in leases or len(leases) < cap
        if granted:
            leases[task_id] = now + LEASE_S
        else:
            waiters.append(task_id)
        if await tasks_repo.put_slots(ws, leases, waiters, expected_version=int(row.get("version") or 0)):
            return granted
    raise RuntimeError("slots row contended")  # retried by the Activity's policy


@activity.defn
async def release_slot(ws: str, task_id: str) -> None:
    for _ in range(8):
        row = await tasks_repo.get_slots(ws)
        leases = {t: e for t, e in (row.get("leases") or {}).items() if t != task_id}
        waiters = [w for w in row.get("waiters") or [] if w != task_id]
        wake, rest = (waiters[0], waiters[1:]) if waiters else (None, [])
        if await tasks_repo.put_slots(ws, leases, rest, expected_version=int(row.get("version") or 0)):
            if wake:
                await get_client().get_workflow_handle(ts.task_workflow_id(ws, wake)).signal("slot_free")
            return
    raise RuntimeError("slots row contended")


# --- attempts --------------------------------------------------------------------------------------


@activity.defn
async def begin_attempt(ws: str, task_id: str, input_trigger_ids: list[str], started: bool = False) -> AttemptRef:
    """Idempotent: an attempt not yet ended is returned as is. Task → working. A task still proposed returns
    `AttemptRef(0, "")`: only Start runs it (the workflow parks the inputs)."""
    tree = await tasks_repo.get_task_tree(ws, task_id)
    if tree["task"]["status"] == "proposed" and not started:
        return AttemptRef(0, "")
    attempts = sorted(tree["attempts"], key=lambda a: a["n"])
    if attempts and attempts[-1]["status"] != "ended":
        last = attempts[-1]
        # A retry after put_attempt landed: finish the move it may have missed (no-op when done).
        await _move(ws, task_id, "working", fields={"attemptCount": last["n"]})
        return AttemptRef(last["n"], last["runId"])
    n = len(attempts) + 1
    kind = _session_kind(ws)
    att, _ = await tasks_repo.put_attempt(
        ws,
        task_id,
        n,
        {
            "agentId": await _agent_for(ws, tree["task"]),
            "sessionId": runtime_transport.session_id_for(ws, task_id if kind == "task" else None),
            "status": "dispatched",
            "runSecret": secrets.token_urlsafe(32),  # binds run events and inputs to this attempt's runtime
            "inputTriggerIds": input_trigger_ids,
            "startedAt": utc_now_iso(),
        },
    )
    await _move(ws, task_id, "working", fields={"attemptCount": n})
    return AttemptRef(n, att["runId"])


async def _request(ws: str, task: dict, att: dict, tree: dict) -> runtime_runs.RunRequest:
    parts = [f"# Task: {task['title']}", task.get("description") or ""]
    for tid in att.get("inputTriggerIds") or []:
        text = await _input_text(ws, tid)
        if text:
            parts.append(f"## New input\n{text}")
    prev = [a for a in tree["attempts"] if a["n"] < att["n"]]
    if prev:
        p = max(prev, key=lambda a: a["n"])
        parts.append(f"## Previous attempt\nAttempt {p['n']} ended: {p.get('outcome') or 'unknown'}.")
    return runtime_runs.RunRequest(input="\n\n".join(x for x in parts if x), instructions=WORKER_INSTRUCTIONS)


@activity.defn
async def worker_turn(inp: WorkerTurnIn) -> WorkerTurnOut:
    """Start the bridge run and hand completion to its run events (async completion)."""
    tree = await tasks_repo.get_task_tree(inp.ws, inp.task_id)
    att = next(a for a in tree["attempts"] if a["n"] == inp.n)
    token = base64.b64encode(activity.info().task_token).decode()
    await tasks_repo.update_attempt(
        inp.ws, inp.task_id, inp.n, {"taskToken": token}, allowed_status=frozenset({"dispatched", "running"})
    )
    request = await _request(inp.ws, tree["task"], att, tree)
    started = time.monotonic()
    while True:
        try:
            status = await runtime_runs.create_run(inp.ws, _identity(inp.ws, inp.task_id, att), request)
            break
        except runtime_runs.RunRefused:
            return WorkerTurnOut("refused", "RunRefused")
        except runtime_runs.RuntimeBusy:
            if time.monotonic() - started > BUSY_RETRY_S:
                return WorkerTurnOut("runtime_lost", "RuntimeBusy")
            activity.heartbeat("busy")
            await asyncio.sleep(BUSY_PAUSE_S)
    if status.status in _TERMINAL:  # a replay of a run that already finished
        await _store_output(inp.ws, inp.task_id, inp.n, status)
        return WorkerTurnOut(_TERMINAL[status.status], None if status.status in _CLEAN else "RunFailed")
    await tasks_repo.update_attempt(
        inp.ws, inp.task_id, inp.n, {"status": "running"}, allowed_status=frozenset({"dispatched", "running"})
    )
    activity.raise_complete_async()


async def _store_output(ws: str, task_id: str, n: int, status: runtime_runs.RunStatus) -> None:
    fields: dict = {"output": (status.output or "")[:65536]}
    if status.usage:
        fields["usage"] = _usage(status.usage)
    await tasks_repo.update_attempt(ws, task_id, n, fields, allowed_status=frozenset({"dispatched", "running"}))


def _usage(raw: dict) -> dict:
    return {
        "inputTokens": int(raw.get("input_tokens") or raw.get("inputTokens") or 0),
        "outputTokens": int(raw.get("output_tokens") or raw.get("outputTokens") or 0),
        "costUsd": float(raw.get("cost_usd") or raw.get("costUsd") or raw.get("estimated_cost_usd") or 0),
    }


@activity.defn
async def reconcile_attempt(ws: str, task_id: str, n: int) -> str:
    """After a lost `worker_turn`: `running` (re-issue the same run id), `terminal:<outcome>` or `lost`."""
    att = await tasks_repo.get_attempt(ws, task_id, n)
    try:
        status = await runtime_runs.get_run(ws, _identity(ws, task_id, att))
    except runtime_runs.RunLost:
        return "lost"
    if status.status in _RUNNING:
        return "running"
    if status.status in _TERMINAL:
        await _store_output(ws, task_id, n, status)
        return f"terminal:{_TERMINAL[status.status]}"
    return "lost"


@activity.defn
async def mark_stop_requested(ws: str, task_id: str, by: str) -> None:
    task = await _task(ws, task_id)
    await tasks_repo.update_task_fields(ws, task_id, {"stopRequestedBy": by}, expected_version=task["version"])


@activity.defn
async def stop_attempt(ws: str, task_id: str, n: int) -> None:
    att = await tasks_repo.get_attempt(ws, task_id, n)
    try:
        await runtime_runs.stop_run(ws, _identity(ws, task_id, att))
    except runtime_runs.RunLost:
        pass


@activity.defn
async def deliver_input(ws: str, task_id: str, n: int, trigger_id: str) -> bool:
    """Steer the live run with one input (§14.10 gate first). Accepted ⇒ 👀 on the person's message (G7).
    Gate refused ⇒ today's copy in the task room and the input is consumed; not accepted ⇒ stays pending."""
    trig = await tasks_repo.get_trigger(ws, trigger_id) or {}
    att = await tasks_repo.get_attempt(ws, task_id, n)
    actor = trig.get("actorUserId")
    if actor:
        gate = await chat_gate.gate_chat(
            user_id=actor, owner_id=ws, session_key=_identity(ws, task_id, att).session_key
        )
        if gate.get("blocked"):
            await task_conversation.post_as_lightbulb(
                ws, ts.task_room_id(task_id), text=gate["message"], event_id=f"task-gate:{task_id}:{trigger_id}"
            )
            return True
    text = await _input_text(ws, trigger_id)
    try:
        accepted = await runtime_runs.steer_run(ws, _identity(ws, task_id, att), text, "input")
    except (runtime_runs.RunLost, runtime_runs.RunRefused, runtime_runs.RuntimeBusy):
        return False
    if accepted:
        await _react_eyes(ws, trig.get("ref") or {})
    return accepted


async def _react_eyes(ws: str, ref: dict) -> None:
    if not (ref.get("roomId") and ref.get("messageId")):
        return
    try:
        msg = await messaging_repo.get_message_by_id(ref["roomId"], ref["messageId"])
        if msg:
            await messaging_repo.set_reaction(
                ws,
                ref["roomId"],
                msg["messageSeq"],
                await task_conversation.front_agent_id(ws),
                "👀",
                True,
                member_kind="agent",
            )
    except Exception:
        logger.warning("👀 reaction failed", exc_info=True)  # cosmetic: never fails the steer


@activity.defn
async def end_attempt(ws: str, task_id: str, n: int, outcome: str, error: str | None) -> None:
    """ATT ended; Task status; slot released; §14.4 relays (result post, failure push, invite ask, usage)."""
    att = await tasks_repo.get_attempt(ws, task_id, n)
    first = att["status"] != "ended"  # a retried end_attempt re-posts idempotently but never re-pushes or re-bills
    if first:
        await tasks_repo.update_attempt(
            ws,
            task_id,
            n,
            {"status": "ended", "outcome": outcome, "error": error, "endedAt": utc_now_iso()},
            allowed_status=frozenset({"dispatched", "running"}),
        )
    tree = await tasks_repo.get_task_tree(ws, task_id)
    task = tree["task"]
    if outcome != "runtime_lost":  # the workflow decides between another attempt and `failed`
        to, reason = _STATUS_AFTER.get(outcome, ("failed", outcome))
        await _move(ws, task_id, to, reason)
    await release_slot(ws, task_id)

    requester = task.get("ownerUserId")
    room, root = _origin(tree)
    if outcome == "completed" and att.get("output"):
        text = att["output"]
        refs = [{"taskId": task_id, "role": "started"}]
        if room:
            await task_conversation.post_as_lightbulb(
                ws,
                room,
                text=text,
                event_id=f"task-result:{task_id}:{n}",
                specialist=task.get("specialist"),
                thread_root=root,
                task_refs=refs,
                kind="result",
            )
        await task_conversation.ensure_task_room(ws, task)
        await task_conversation.post_as_lightbulb(
            ws,
            ts.task_room_id(task_id),
            text=text,
            event_id=f"task-result-room:{task_id}:{n}",
            specialist=task.get("specialist"),
            kind="result",
        )
        if room:
            await messaging.ask_who_should_see(ws, {"roomId": room}, "completed")
    if not first:
        return
    if outcome in ("failed", "refused", "unavailable", "checkpoint_failed") and requester:
        await push_service.send_push_to_user(
            requester, "Lightbulb", f"{task['title']} didn't finish.", {"taskId": task_id}
        )
    usage = att.get("usage")
    if usage and requester:
        await messaging._record_run_usage(
            ws,
            requester,
            messaging.RunUsage(
                model="task",
                input_tokens=int(usage.get("inputTokens") or 0),
                output_tokens=int(usage.get("outputTokens") or 0),
                cache_read_tokens=0,
                cache_write_tokens=0,
                estimated_cost_usd=usage.get("costUsd"),
            ),
            room_turn=False,
            event_kind="task",
        )


@activity.defn
async def transition(ws: str, task_id: str, to: str, reason: str | None) -> None:
    """A status change the workflow decides (resolved quiet/dismissed/stopped, failed after lost attempts)."""
    try:
        await _move(ws, task_id, to, reason)
    except tasks_repo.IllegalTransition:
        task = await _task(ws, task_id)
        if task["status"] != to:
            raise


ACTIVITIES = [
    check_budget,
    acquire_slot,
    release_slot,
    begin_attempt,
    worker_turn,
    reconcile_attempt,
    mark_stop_requested,
    stop_attempt,
    deliver_input,
    end_attempt,
    transition,
]
