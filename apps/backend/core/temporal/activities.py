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
import uuid
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import container_repo, messaging_repo, tasks_repo
from core.services import (
    chat_gate,
    container_provision,
    hermes_profiles,
    memory_tags,
    memory_tasks_index,
    messaging,
    push_service,
    runtime_runs,
    runtime_transport,
    source_replies,
    super_agent_faults,
    task_conversation,
)
from core.temporal import client as temporal_client
from core.temporal import router_prompt, rules_activities
from core.temporal.client import get_client
from core.temporal.types import AttemptRef, RouterTurnIn, RouterTurnOut, TaskInputRef, WorkerTurnIn, WorkerTurnOut

logger = logging.getLogger(__name__)

SHARED_VM_SLOTS = 3  # contracts §9 / C7
LEASE_S = int(timedelta(hours=7).total_seconds())  # outlives worker_turn's 6 h 30 m
BUSY_RETRY_S = 600
BUSY_PAUSE_S = 15
INPUT_MAX = 32 * 1024

WORKER_INSTRUCTIONS = (
    "You are working on one task for this workspace. Do the work, then answer with the result the person "
    "asked for. Post notes only in this task's own room; Lightbulb posts your result for you. For work that "
    "belongs to a teammate or a person, follow the lightbulb skill's references/tasks.md."
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
    """Transition unless already there; walks queued first when the edge needs it. A task that ends (resolved,
    failed) lets go of its threads, so later replies there go to the Router (and may start a new task); a retry or
    reopen takes back the ones no other task has claimed since."""
    task = await _task(ws, task_id)
    if task["status"] != to:
        from core.temporal import learning  # Lane V: overlay expiry follows the task's terminal state

        if task["status"] in learning.TERMINAL and to not in learning.TERMINAL:
            await learning.set_overlay_tag(ws, task_id, on=False)  # reopen: untag BEFORE the next attempt
            # Retry/reopen takes its threads back before the move (a crash in between redoes both), so a reply
            # in the original thread reaches this task again instead of starting a second one.
            await tasks_repo.resubscribe_router_threads(ws, task_id)
        if task["status"] not in tasks_repo.TRANSITIONS[to] and "queued" in tasks_repo.TRANSITIONS[to]:
            # e.g. a retried failed/reopened task: failed → queued → working, or → waiting when over budget
            await tasks_repo.transition_task(ws, task_id, to="queued")
        row = await tasks_repo.transition_task(ws, task_id, to=to, reason=reason, fields=fields)
        await memory_tasks_index.index_task(ws, row)  # memory (M5): the task card follows every status change
        if to in learning.TERMINAL:
            await learning.set_overlay_tag(ws, task_id, on=True)
    if to in ("resolved", "failed"):  # also on a retry that finds the move done (idempotent)
        await tasks_repo.unsubscribe_all(ws, task_id)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


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
    return runtime_runs.RunIdentity.for_attempt(ws, task_id, att)


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


async def _session_kind(ws: str) -> str:
    """V3's `session_kind_for` (async: it reads the switch and the custody gate); the shared VM until it lands."""
    fn = getattr(runtime_runs, "session_kind_for", None)
    return await fn(ws) if fn else "workspace"


@activity.defn
async def acquire_slot(ws: str, task_id: str) -> bool:
    """Take a lease (cap per VM kind). Full ⇒ join the waiters and return False; `release_slot` signals."""
    cap = _cap(await _session_kind(ws))
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
    kind = await _session_kind(ws)
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
    # Lane Q (contracts §8): every Worker run carries the effect checkpoint; the bridge refuses `required`
    # until its image has F1, so this lands after that dev roll.
    return runtime_runs.RunRequest(
        input="\n\n".join(x for x in parts if x),
        instructions=WORKER_INSTRUCTIONS,
        memory=memory_tags.block_for_worker(task),  # memory (M2): the Worker writes in the task's lane
        effects="required",
    )


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
    fake_ms = super_agent_faults.fault("fake_worker")
    if fake_ms is not None:  # dev fault hook: no bridge run, a canned result after `ms`
        await asyncio.sleep(int(fake_ms or 0) / 1000)
        canned = runtime_runs.RunStatus(
            att["runId"], "completed", f"Done (fake worker): {tree['task']['title']}", None, None, []
        )
        await _store_output(inp.ws, inp.task_id, inp.n, canned)
        return WorkerTurnOut("completed", None)
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
    if blocked := await task_conversation.gate_input(ws, task_id, trig):
        await task_conversation.post_as_lightbulb(
            ws, ts.task_room_id(task_id), text=blocked, event_id=f"task-gate:{task_id}:{trigger_id}", remember=False
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
    # Lane V: a task-session attempt (V3's session kind) stops its own VM and queues its learning
    # merge (`learn:{ws}:{specialist}`); nothing for a shared-VM attempt. Idempotent on a retry.
    from core.temporal import learning

    await learning.close_task_attempt_if_task_session(ws, task_id, att, n)

    requester = task.get("ownerUserId")
    room, root = _origin(tree)
    if room and not _is_uuid(room):
        # A deliver room that is not a room id (a name the Router saved, gate run 3) can never take a post: the
        # task room still gets the result, and the attempt still ends (its automation claim is freed).
        logger.warning("end_attempt: origin room is not a room id task=%s", task_id)
        room = None
    if outcome == "completed" and att.get("output"):
        text = att["output"]
        files = att.get("attachments") or []  # PUT by the task VM to the TASK room's keys
        refs = [{"taskId": task_id, "role": "started"}]
        # The task room FIRST: it adopts the files. Only once they are attached does the origin post say so;
        # an adoption the backend refuses (bytes, size, missing object) posts the result without them.
        await task_conversation.ensure_task_room(ws, task)
        result_room = dict(
            text=text, event_id=f"task-result-room:{task_id}:{n}", specialist=task.get("specialist"), kind="result"
        )
        try:
            await task_conversation.post_as_lightbulb(
                ws, ts.task_room_id(task_id), **result_room, uploaded_attachments=files
            )
        except messaging.ApiProblem:
            if not files:
                raise
            logger.warning("task result files not attached task=%s attempt=%s", task_id, n)
            files = []
            await task_conversation.post_as_lightbulb(ws, ts.task_room_id(task_id), **result_room)
        if room:
            names = ", ".join(f["fileName"] for f in files)
            try:
                await task_conversation.post_as_lightbulb(
                    ws,
                    room,
                    text=f"{text}\n\n_(Attached in the task: {names}.)_" if files else text,
                    event_id=f"task-result:{task_id}:{n}",
                    specialist=task.get("specialist"),
                    thread_root=root,
                    task_refs=refs,
                    kind="result",
                )
                await messaging.ask_who_should_see(ws, {"roomId": room}, "completed")
            except messaging.ApiProblem:
                # A deleted or left room: the task room has the result, and the attempt must still end (a
                # failed end_attempt fails the workflow and strands an automation's run claim).
                logger.warning("end_attempt: origin room refused the result task=%s attempt=%s", task_id, n)
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


# --- Router ----------------------------------------------------------------------------------------

ROUTER_POLL_S = 3.0
ROUTER_MAX_ITERATIONS = 30
ROUTER_BUDGET_S = 240  # under the Activity's 5 min start-to-close (workflows._ROUTER_TURN)
_OPEN_STATUSES = ("working", "waiting", "idle", "queued")
_OPEN_SCAN = 20
_CLOSED_STATUSES = ("resolved", "failed")
_CLOSED_SCAN = 10  # shown
_CLOSED_PAGE = 50  # read per query
_CLOSED_MAX_ROWS = 500  # read per status per Router turn
_CLOSED_WITHIN = timedelta(days=1)  # "Recently closed": what a status question can still be about


async def _trigger_view(ws: str, trig: dict) -> dict:
    """What the Router reads about one trigger: who said what, where (text from the message for chat)."""
    ref = trig.get("ref") or {}
    view = {
        "triggerId": trig["triggerId"],
        "source": trig["source"],
        "admission": trig["admission"],
        "pinnedSpecialist": ref.get("pinnedSpecialist"),
        "routedTo": trig.get("routedTo") or {},
        "threaded": bool(ref.get("threadRoot")),
        "text": await _input_text(ws, trig["triggerId"]),
        "who": None,
        "where": None,
    }
    if ref.get("roomId") and ref.get("messageId"):
        msg = await messaging_repo.get_message_by_id(ref["roomId"], ref["messageId"]) or {}
        meta = await messaging_repo.get_room(ref["roomId"]) or {}
        view["who"] = (msg.get("author") or {}).get("displayName")
        view["where"] = "a direct message" if meta.get("kind") == "dm" else f"#{meta.get('name') or 'room'}"
    else:
        view["who"] = str(ref.get("from") or trig["source"])
    return view


_RECENT = 10


async def _recent_lines(ws: str, trig: dict, batch: set[str], lane: str, user_id: str | None) -> list[dict]:
    """The room's lines just before the batch's first message, each Lightbulb line with the task cards it really
    carried (`taskRefs`): the Router's view of what was said and what was started, instead of its own transcript."""
    ref = trig.get("ref") or {}
    if not (ref.get("roomId") and ref.get("messageId")):
        return []
    msg = await messaging_repo.get_message_by_id(ref["roomId"], ref["messageId"])
    if not msg or not msg.get("messageSeq"):
        return []
    front = await task_conversation.front_agent_id(ws)
    seq = int(msg["messageSeq"])
    if ref.get("threadRoot"):  # a follow-up in a thread ("make it shorter") sees its thread, not the room
        rows = (await messaging_repo.list_thread(ref["roomId"], ref["threadRoot"], seq))[-_RECENT:]
    else:
        rows = await messaging_repo.list_messages_before(ref["roomId"], seq, _RECENT)
    rows = [
        r
        for r in rows
        if not (r.get("deletedAt") or r.get("progress") or ts.trigger_id("chat", r.get("messageId", "")) in batch)
    ]
    # Each card's task as it is now (one older than a day, or past the Open scan, is in neither task list), in one
    # read; a task outside this trigger's scope (a person's private task) shows no state.
    ids = {c["taskId"] for r in rows for c in r.get("taskRefs") or [] if c.get("taskId")}
    room = ts.trigger_room(trig)
    tasks = {
        t["taskId"]: t
        for t in await tasks_repo.batch_get(ws, [ts.task_sk(i) for i in ids])
        if _task_in_scope(t, lane, user_id, room)
    }
    out = []
    for row in rows:
        author = row.get("author") or {}
        cards = [
            {**c, "state": router_prompt.status_words(tasks[c["taskId"]], user_id)} if c.get("taskId") in tasks else c
            for c in row.get("taskRefs") or []
        ]
        out.append(
            {
                "who": author.get("displayName"),
                "text": row.get("editedText") or row.get("text") or "",
                "lightbulb": author.get("kind") == "agent" and author.get("id") == front,
                "cards": cards,
            }
        )
    return out


def _task_in_scope(task: dict, lane: str, user_id: str | None, room_id: str | None) -> bool:
    """May a trigger from (`lane`, `user_id`, `room_id`) pass through to `task`? Private lane: only person tasks
    naming the user. Shared lane: workspace tasks, and room tasks of that same room (never another room's)."""
    audience = task.get("audience") or {}
    if lane != "shared":
        return audience.get("kind") == "person" and bool(user_id) and user_id in (audience.get("userIds") or [])
    if audience.get("kind") == "workspace":
        return True
    return audience.get("kind") == "room" and bool(room_id) and audience.get("roomId") == room_id


async def _open_tasks(ws: str, lane: str, user_id: str | None, room_ids: set[str]) -> list[dict]:
    """Tasks the Router may pass a batch's messages to (`_task_in_scope` for any of the batch's rooms)."""
    rows: list[dict] = []
    for status in _OPEN_STATUSES:
        page, _ = await tasks_repo.list_tasks_by_status(ws, status, limit=_OPEN_SCAN)
        rows += [r for r in page if not r.get("hidden")]
    return [r for r in rows if any(_task_in_scope(r, lane, user_id, room) for room in room_ids or {None})]


async def _closed_tasks(ws: str, lane: str, user_id: str | None, room_ids: set[str]) -> list[dict]:
    """The batch's scope's tasks closed in the last day, newest first: shown with their state, never passed to."""
    since = (datetime.now(timezone.utc) - _CLOSED_WITHIN).isoformat()
    rows: list[dict] = []
    for status in _CLOSED_STATUSES:
        # `by-status` is sorted by updatedAt, newest first: page until a row is older than the day (other scopes'
        # closures never crowd this scope's out of a fixed-size first page).
        cursor: str | None = None
        seen = 0
        while True:
            page, cursor = await tasks_repo.list_tasks_by_status(ws, status, limit=_CLOSED_PAGE, cursor=cursor)
            seen += len(page)
            fresh = [r for r in page if (r.get("updatedAt") or "") >= since]
            rows += [r for r in fresh if not r.get("hidden")]
            # ponytail: at most _CLOSED_MAX_ROWS rows read per status per Router turn; past that a busy workspace's
            # day of closures is cut short (oldest first). Add a per-room index if that ever matters.
            if not cursor or len(fresh) < len(page) or seen >= _CLOSED_MAX_ROWS:
                break
    rows = [r for r in rows if any(_task_in_scope(r, lane, user_id, room) for room in room_ids or {None})]
    return sorted(rows, key=lambda r: r.get("updatedAt") or "", reverse=True)[:_CLOSED_SCAN]


async def _cheap_model(ws: str) -> str | None:
    """The cheaper tier for Router turns (contracts §9); None keeps the profile's default."""
    choice, byo = await container_provision.resolve_provider_choice(ws)
    return hermes_profiles._strip_provider_prefix(hermes_profiles.cheap_model_ref(choice, byo))


async def _router_run(ws: str, inp: RouterTurnIn, prompt: str, memory: dict, scope: str) -> runtime_runs.RunStatus:
    """One Router run in the lane's session, polled to its end (Router runs post no events)."""
    identity = runtime_runs.RunIdentity(
        ts.router_run_id(ws, inp.batch_id),
        ws,
        inp.batch_id,
        1,
        await task_conversation.front_agent_id(ws),
        mode="router",
        lane=inp.lane,
        room=ts.router_room_key(scope),  # the batch's conversation: a batch is one (lane, trigger_scope)
        model=await _cheap_model(ws),
    )
    request = runtime_runs.RunRequest(
        input=prompt,
        instructions=router_prompt.ROUTER_INSTRUCTIONS,
        max_iterations=ROUTER_MAX_ITERATIONS,
        run_budget_seconds=ROUTER_BUDGET_S,
        memory=memory,
    )
    try:
        status = await runtime_runs.create_run(ws, identity, request)
    except runtime_runs.RunRefused:
        # A retry after a partial apply renders a different prompt (open tasks changed) and the bridge
        # fingerprints the whole request: the stored run (same run id) already holds the decisions.
        status = await runtime_runs.get_run(ws, identity)
    started = time.monotonic()
    while status.status in _RUNNING:
        if time.monotonic() - started > ROUTER_BUDGET_S:
            raise TimeoutError("router run did not finish in time")
        activity.heartbeat("router")
        await asyncio.sleep(ROUTER_POLL_S)
        status = await runtime_runs.get_run(ws, identity)
    return status


def _scripted(trig: dict) -> ts.StartTask | None:
    """A trigger whose `ref.start` is a `StartTask` JSON (automations, C31) is applied without a model turn."""
    start = (trig.get("ref") or {}).get("start")
    if not isinstance(start, dict):
        return None
    try:  # the fire's own kind/triggerIds (a `StartTask` dump) are this trigger's by construction
        return ts.StartTask.model_validate({**start, "kind": "start_task", "triggerIds": [trig["triggerId"]]})
    except ValueError:
        logger.warning("trigger %s carries an invalid ref.start", trig["triggerId"])
        return None


def _thread_key(trig: dict) -> str | None:
    ref = trig.get("ref") or {}
    if ref.get("roomId") and (ref.get("threadRoot") or ref.get("messageId")):
        return f"room:{ref['roomId']}:{ref.get('threadRoot') or ref['messageId']}"
    return None


async def _room_exists(room_id: str) -> bool:
    try:
        uuid.UUID(room_id)
    except ValueError:
        return False
    return await messaging_repo.get_room(room_id) is not None


async def _deliver(
    ws: str, trig: dict, text: str, *, event_id: str, refs: list[dict] | None, remember: bool = True
) -> str | None:
    """Post in the trigger's place: its thread or room (chat), the deliver room (other sources), or back to the
    sender for a plain mail/text reply. Idempotent on `event_id`."""
    ref = trig.get("ref") or {}
    if trig["source"] in ("mail", "text") and not refs:
        # A plain reply goes back the way it came (lane A's sender), idempotent on `event_id`.
        await source_replies.deliver_reply(ws, trig, text, idempotency_key=event_id)
        return None
    room = ts.trigger_room(trig)
    if not room or not await _room_exists(room):
        # A deliver room that is not a room (gate run 2: an automation saved with `room:acc-a1-…`) must not fail
        # the batch: its other triggers' tasks still start, and this trigger's task still runs.
        logger.warning("trigger %s has nowhere to answer (source %s)", trig["triggerId"], trig["source"])
        return None
    return await task_conversation.post_as_lightbulb(
        ws, room, text=text, event_id=event_id, thread_root=ref.get("threadRoot"), task_refs=refs, remember=remember
    )


async def _ensure_task(ws: str, task_id: str, trig: dict, lane: str, status: str, **fields) -> dict:
    """The task a decision names, created once (deterministic id) with its room and memory card."""
    existing = await tasks_repo.get_task(ws, task_id)
    if existing:
        return existing
    ref = trig.get("ref") or {}
    actor = trig.get("actorUserId")
    room = ts.trigger_room(trig)
    private = lane != "shared"
    asked = fields.pop("visibility", None)
    # Clamp the model's `visibility` to what the trigger allows (§5): a private lane is always the person's;
    # a room trigger is that room's or the person's, never the workspace's; workspace only with no room.
    if private:
        kind = "person"
    elif room:
        kind = "person" if asked == "person" else "room"
    else:
        kind = "person" if asked == "person" else "workspace"
    if kind == "person" and not actor:
        # ponytail: refuse rather than widen (lead ruling). Lane A's fires pass the schedule owner as the actor.
        raise ValueError(f"trigger {trig['triggerId']} names no actor for a person-visible task")
    audience = {"kind": kind}
    if kind == "person":
        audience["userIds"] = [actor]
    elif kind == "room":
        audience["roomId"] = room
    row = {
        "taskId": task_id,
        "identifier": await tasks_repo.next_identifier(ws),
        "status": status,
        "audience": audience,
        "lane": "private" if private else "shared",
        "ownerUserId": actor,
        "triggerId": trig["triggerId"],
        "a2aTaskId": ref.get("a2aTaskId"),  # lane Q (Q10): an inbound A2A task reports back by this id
        **{k: v for k, v in fields.items() if v is not None},
    }
    row = {k: v for k, v in row.items() if v is not None}
    row, _ = await tasks_repo.create_task(ws, row)
    await task_conversation.ensure_task_room(ws, row)
    await memory_tasks_index.index_task(ws, row)  # memory (M5)
    return row


async def _subscribe(ws: str, task_id: str, thread_key: str | None) -> None:
    if not thread_key:
        return
    try:
        await tasks_repo.subscribe(ws, task_id, thread_key, created_by="router")
    except tasks_repo.TasksRepoError:
        # ponytail: one ack message carries several cards (G2), and a thread belongs to one task; the first
        # task takes the ack thread, the others answer top-level in the room (`_origin`'s audience fallback).
        logger.info("thread %s already belongs to another task; %s not subscribed", thread_key, task_id)


async def _apply(
    ws: str, inp: RouterTurnIn, triggers: dict[str, dict], decisions: list, run_text: str | None
) -> RouterTurnOut:
    """Apply validated decisions idempotently: one Router message per trigger (G2), tasks keyed by
    `router_task_id(batch, i, k)`, event ids `router:{batch}:{i}`; returns ids only."""
    slots: dict[str, dict] = {}
    covered: set[str] = set()
    routed_to: dict[str, list[str]] = {}  # every trigger a start or pass_through covers → its tasks, in order

    def slot(tid: str) -> dict:
        return slots.setdefault(
            tid, {"lines": [], "refs": [], "starts": [], "proposed": [], "passes": [], "acks": set()}
        )

    def ack(sl: dict, line: str | None) -> None:
        """One ack line per trigger: several start/propose/pass_through decisions on one ask are one message with
        a card per task, and the model writes the same summary on each (dev, 5 Nov: the line twice). The first
        decision's ack is the message's line."""
        if line and not sl["acks"]:
            sl["lines"].append(line)
        if line:
            sl["acks"].add(line)

    for i, dec in enumerate(decisions):
        primary = dec.trigger_ids[0]
        if not isinstance(dec, ts.Ignore):  # `ignore` on a respond trigger is silence: the fallback line covers it
            covered.update(dec.trigger_ids)
        trig, sl = triggers[primary], slot(primary)
        if isinstance(dec, ts.StartTask):
            # A scripted start (`ref.start`, lane A's fires) is keyed by its trigger, so the automation knows the
            # task id before the Router applies it (`ref.taskId`); a model decision by the batch.
            scripted = isinstance((trig.get("ref") or {}).get("start"), dict)
            task_id = ts.router_task_id(primary, 0, 0) if scripted else ts.router_task_id(inp.batch_id, i, 0)
            gate = await chat_gate.gate_chat(
                user_id=trig.get("actorUserId") or "", owner_id=ws, session_key=f"task:{task_id}"
            )
            if gate.get("blocked"):  # §14.10: today's failure copy, no task
                sl["lines"].append(gate.get("message") or router_prompt.FALLBACK)
                continue
            try:
                await _ensure_task(
                    ws,
                    task_id,
                    trig,
                    inp.lane,
                    "queued",
                    title=dec.title,
                    description=dec.brief,
                    specialist=dec.specialist,
                    visibility=dec.visibility,
                    parentTaskId=dec.parent_task_id,
                    policyRef=dec.policy_ref,
                    budgetUsd=dec.budget_usd,
                    previousRunTaskId=dec.previous_run_task_id,
                )
            except ValueError:  # a person-visible task with nobody to see it: this decision only
                logger.warning("start_task dropped for trigger %s", primary, exc_info=True)
                sl["lines"].append(router_prompt.FALLBACK)
                continue
            ack(sl, dec.ack)
            sl["refs"].append({"taskId": task_id, "role": "started"})
            sl["starts"].append(task_id)
            for tid in dec.trigger_ids:
                routed_to.setdefault(tid, []).append(task_id)
        elif isinstance(dec, ts.ProposeTasks):
            for k, t in enumerate(dec.tasks):
                task_id = ts.router_task_id(inp.batch_id, i, k)
                try:
                    await _ensure_task(
                        ws,
                        task_id,
                        trig,
                        inp.lane,
                        "proposed",
                        title=t.title,
                        description=t.brief,
                        specialist=t.specialist,
                        proposalReason=dec.reason,
                    )
                except ValueError:
                    logger.warning("proposed task dropped for trigger %s", primary, exc_info=True)
                    continue
                sl["refs"].append({"taskId": task_id, "role": "suggested"})
                sl["proposed"].append(task_id)
            ack(sl, dec.ack)
        elif isinstance(dec, ts.PassThrough):
            if blocked := await task_conversation.gate_input(ws, dec.task_id, trig):  # §14.10, like a start
                sl["lines"].append(blocked)
                continue
            if (trig.get("routedTo") or {}).get("taskId") != dec.task_id:
                # Once per trigger and task: a retried router_turn reads the routedTo it recorded and never
                # signals again (the workflow dedupes in its run; this also covers a run that ended in between).
                await temporal_client.signal_task_input(ws, dec.task_id, TaskInputRef(primary, "message"))
                await tasks_repo.set_trigger_routed_to(ws, primary, {"taskId": dec.task_id})
            sl["passes"].append(dec.task_id)
            for tid in dec.trigger_ids:
                routed_to.setdefault(tid, []).append(dec.task_id)
            ack(sl, dec.ack)
        elif isinstance(dec, ts.Ask):
            sl["lines"].append(dec.text + (f" ({' / '.join(dec.options)})" if dec.options else ""))
        elif isinstance(dec, ts.Reply):
            sl["lines"].append(dec.text)
        activity.heartbeat("apply")
    for tid, trig in triggers.items():  # a `respond` trigger is never left in silence
        if tid not in covered and trig["admission"] == "respond":
            slot(tid)["lines"].append(run_text or router_prompt.FALLBACK)

    started: list[str] = []
    posted: list[str] = []
    failed: list[str] = []
    for idx, tid in enumerate(inp.trigger_ids):
        sl, trig = slots.get(tid), triggers.get(tid)
        if not sl or not trig:
            continue
        message_id = None
        if sl["lines"] or sl["refs"]:  # a pass_through without an ack posts nothing (routed below)
            text = "\n".join(line for line in sl["lines"] if line) or "On it."
            message_id = await _deliver(
                ws,
                trig,
                text,
                event_id=f"router:{inp.batch_id}:{idx}",
                refs=sl["refs"] or None,
                # Acks and the fallback line are boilerplate: a post of only those stays out of room memory.
                remember=any(
                    line and line not in sl["acks"] and line != router_prompt.FALLBACK for line in sl["lines"]
                ),
            )
        if message_id:
            posted.append(message_id)
        ref = trig.get("ref") or {}
        ack_thread = (
            f"room:{ref['roomId']}:{ref.get('threadRoot') or message_id}" if ref.get("roomId") and message_id else None
        )
        for task_id in sl["starts"] + sl["proposed"]:
            await _subscribe(ws, task_id, ack_thread)
        for task_id in sl["starts"]:
            try:
                await temporal_client.start_task_workflow(ws, task_id, "router")
            except Exception:  # noqa: BLE001 -- one bad start never skips the batch's other starts
                logger.warning("start_task_workflow failed for task %s", task_id, exc_info=True)
                failed.append(task_id)
                continue
            started.append(task_id)
    # Recorded for what did start, before any raise: a second failure never loses them, and apply_fallback sees a
    # trigger whose cards are already posted.
    for tid, task_ids in routed_to.items():
        # The first task that did start (a trigger fanned out to A, failed, and B, started, records B).
        task_id = next((t for t in task_ids if t not in failed), None)
        if task_id is None:
            continue
        await tasks_repo.set_trigger_routed_to(ws, tid, {"taskId": task_id})
        if _thread_key(triggers[tid]):
            await rules_activities.note_task_started(ws, _thread_key(triggers[tid]))  # rules (S6): did it yourself
    if failed:
        # After every other start went out: the Activity retries, and a retried start is a no-op for those
        # already running (USE_EXISTING), so the failed ones get another go instead of sitting queued.
        raise RuntimeError(f"start_task_workflow failed for {failed}")
    return RouterTurnOut(inp.batch_id, started, posted)


#: `router_turn`'s error type while the workspace's runtime is still starting: the Router waits, it never falls back.
RUNTIME_NOT_READY = "RuntimeNotReady"


async def _runtime_ready(ws: str) -> bool:
    """The runtime can take a run: the same rule `/container/status` reports as ready (readiness_state)."""
    from core.services.readiness_state import resolve_runtime_phase

    return resolve_runtime_phase(await container_repo.get_by_owner_id(ws)) == "ready"


@activity.defn
async def router_turn(inp: RouterTurnIn) -> RouterTurnOut:
    """Render the batch, run the Router (cheap model, read-only memory), apply its decisions. Retry-safe: the
    bridge replays the run (same run id), tasks and posts are keyed deterministically."""
    ws = inp.ws
    triggers: dict[str, dict] = {}
    for tid in inp.trigger_ids:
        trig = await tasks_repo.get_trigger(ws, tid)
        if trig:
            triggers[tid] = trig
    if not triggers:
        return RouterTurnOut(inp.batch_id)
    decisions: list = []
    to_model: dict[str, dict] = {}
    for tid, trig in triggers.items():
        scripted = _scripted(trig)
        if scripted:
            decisions.append(scripted)
        else:
            to_model[tid] = trig
    run_text: str | None = None
    if to_model and super_agent_faults.fault("router_turn_timeout") is not None:  # dev fault hook
        raise TimeoutError("fault: router_turn_timeout")
    script = super_agent_faults.fault("scripted_router") if to_model else None
    if to_model and script:  # dev fault hook: decisions by exact message text, no Router run
        roster = await messaging.load_agent_roster(ws)
        user_id = next((t.get("actorUserId") for t in to_model.values() if t.get("actorUserId")), None)
        rooms = {ts.trigger_room(t) for t in to_model.values()} - {None}
        open_tasks = await _open_tasks(ws, inp.lane, user_id, rooms)
        texts = {tid: await _input_text(ws, tid) for tid in to_model}
        decisions += router_prompt.validate_decisions(
            await super_agent_faults.scripted_decisions(script, texts),
            trigger_ids=set(to_model),
            roster_ids={a["id"] for a in roster},
            open_tasks={t["taskId"]: t for t in open_tasks},
            scope=lambda tid: (
                inp.lane,
                to_model[tid].get("actorUserId"),
                ts.trigger_room(to_model[tid]),
            ),
            in_scope=_task_in_scope,
        )
    elif to_model:
        # A message sent while the workspace is still being set up is admitted and kept: the Router workflow waits
        # for the runtime and answers once it is up (no fallback reply for "not started yet").
        if not await _runtime_ready(ws):
            raise ApplicationError(
                "the workspace runtime is still starting", type=RUNTIME_NOT_READY, non_retryable=True
            )
        private = inp.lane != "shared"
        user_id = next((t.get("actorUserId") for t in to_model.values() if t.get("actorUserId")), None)
        room_id = next((r for r in map(ts.trigger_room, to_model.values()) if r), None)
        roster = await messaging.load_agent_roster(ws)
        rooms = {ts.trigger_room(t) for t in to_model.values()} - {None}
        open_tasks = await _open_tasks(ws, inp.lane, user_id, rooms)
        views = [await _trigger_view(ws, t) for t in to_model.values()]
        # The batch is one conversation (lane, trigger_scope): its lines come from the first trigger that has a
        # message in a room. A batch with none (mail, schedules) has no stored lines; the prompt says so.
        chat = next((t for t in to_model.values() if {"roomId", "messageId"} <= set(t.get("ref") or {})), None)
        recent = await _recent_lines(ws, chat, set(to_model), inp.lane, user_id) if chat else None
        closed = await _closed_tasks(ws, inp.lane, user_id, rooms)
        prompt = router_prompt.render_batch(
            views, roster, open_tasks, lane=inp.lane, recent=recent, closed=closed, asker=user_id
        )
        memory = memory_tags.block_for_router(
            "private" if private and user_id else "shared", user_id if private else None, room_id
        )
        status = await _router_run(ws, inp, prompt, memory, ts.trigger_scope(next(iter(to_model.values()))))
        if status.status != "completed":
            raise RuntimeError(f"router run {status.status}: {status.error or ''}")
        # The run's prose answers a trigger only when the Router made no decision at all: with decisions it is a
        # summary of them ("Growth has the checklist"), which must never stand in for a decision that was dropped.
        run_text = None if status.decisions else (status.output or "").strip() or None
        decisions += router_prompt.validate_decisions(
            status.decisions,
            trigger_ids=set(to_model),
            roster_ids={a["id"] for a in roster},
            open_tasks={t["taskId"]: t for t in open_tasks},
            scope=lambda tid: (
                inp.lane,
                to_model[tid].get("actorUserId"),
                ts.trigger_room(to_model[tid]),
            ),
            in_scope=_task_in_scope,
        )
    return await _apply(ws, inp, triggers, decisions, run_text)


@activity.defn
async def apply_fallback(ws: str, trigger_ids: list[str]) -> None:
    """`router_turn` failed past its retries: one plain fallback reply per `respond` trigger (spec 01 §3.5)."""
    for tid in trigger_ids:
        trig = await tasks_repo.get_trigger(ws, tid)
        if trig and trig["admission"] == "respond":
            # Work already started for it (its ack and cards are posted): say only that the rest did not start.
            line = router_prompt.PARTIAL if (trig.get("routedTo") or {}).get("taskId") else router_prompt.FALLBACK
            await _deliver(ws, trig, line, event_id=f"router-fallback:{tid}", refs=None, remember=False)


ACTIVITIES = [
    router_turn,
    apply_fallback,
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
