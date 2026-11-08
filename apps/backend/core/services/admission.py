"""Admission: which messages and source events reach the Router (contracts-v2 §10, §14.7; wave-2 O Task 6).

Every source writes one `WORK#ADMIT#` outbox row (a Router room's human message, in `create_message`'s
transaction; other sources through `enqueue_trigger`). The FIFO consumer (`openclaw_work`) calls
`admit_pointer`, which, in order:

1. reads META + MSG (a message) or the TRIG row (another source);
2. thread pass-through: a reply on a thread an open task subscribes to, or a message in a task's own room,
   goes to that task (`signal_task_input`);
3. fast path in code: a DM, an @mention or name-address (the MSG's `user_request` targets) ⇒ `respond`;
   Lightbulb's per-room listen mode: `observe` ⇒ nothing, `mentions` ⇒ addressed only;
4. unaddressed ⇒ one Jev call (`jev.route_for_router`): Router or nobody, and which specialist fits;
   Jev down ⇒ addressed only (§14.7);
5. records the TRIG row (deterministic id: a redelivery is a replay) and signal-with-starts the Router.

Temporal down ⇒ `WorkRetry` (the FIFO redelivers).
"""

from __future__ import annotations

import logging
import re

from temporalio.service import RPCError

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric
from core.repositories import automations_repo, messaging_repo, tasks_repo
from core.services import jev, messaging, room_memory, rules, task_conversation, tasks_access
from core.services.openclaw_work import WorkRetry
from core.temporal import client as temporal
from core.temporal.types import TaskInputRef, TriggerRef

logger = logging.getLogger(__name__)

_HISTORY = 20
# Lightbulb spoken to (a vocative at the start: "Hi Lightbulb, …", "Lightbulb, can you…", "lightbulb?"), not the
# company's name mid-sentence ("the Lightbulb app is down"), which goes to Jev like any unaddressed message.
_LIGHTBULB_NAME = re.compile(r"^\W*(?:(?:hey|hi|hello|yo|ok|okay)\s+)?lightbulb\b\s*[,:?!]", re.IGNORECASE)


VOICE_HINT = (
    "(Said aloud in a voice talk; the reply is read out. Answer in one or two short spoken sentences: no lists, "
    "tables, links or markdown. In them, name the specific items you found (the PR numbers, the figures, the "
    "names), never a summary of themes. Longer work goes in its thread as usual.)"
)


async def router_room(meta: dict, owner_id: str) -> bool:
    """A room people chat with Lightbulb in: Lightbulb (the front agent) seated, and not an
    app/feed/vendor/session room or one shared with another organization. A task's own room is a Router room, and so
    is a Google Meet's: the meeting's voice posts the lines addressed to the assistant there (voice_meet `/ask`). A
    room without Lightbulb's seat (a specialist's DM) keeps today's path: Lightbulb could not answer there."""
    if not meta:
        return False
    if meta.get("task"):
        return True
    if (
        not meta.get("roomId")
        or meta.get("app")
        or messaging.is_session_room(meta)
        or messaging.is_feed_room(meta)
        or messaging.is_vendor_room(meta)
    ):
        return False
    if await _front_member(meta["roomId"], owner_id) is None:
        return False
    return not await messaging_repo.has_foreign_members(meta["roomId"], owner_id)


def lane_for(meta: dict, author: dict) -> str:
    """A DM is the person's private lane; every other room shares one lane."""
    if meta.get("kind") == "dm" and author.get("kind") == "human":
        return ts.router_lane_key(author["id"])
    return "shared"


async def enqueue_trigger(ws: str, trigger: ts.Trigger, *, room_id: str | None = None) -> bool:
    """Record a non-message trigger and its one admission row. False: already enqueued (same trigger id).
    `room_id`: where Lightbulb answers it, stored on the trigger's ref."""
    item = trigger.model_dump(by_alias=True)
    if room_id:
        item["ref"] = {**item.get("ref", {}), "deliverRoomId": room_id}
    _, created = await tasks_repo.put_trigger(ws, item)
    # Unconditional: a crash between the two puts would otherwise strand the TRIG row for ever. A re-put
    # of the identical row is a MODIFY, which the outbox Pipe's INSERT filter ignores.
    await messaging_repo.put_admit_trigger(ws, trigger.trigger_id)
    return created


async def admit_pointer(pointer: dict) -> None:
    if pointer["kind"] == "admit_trigger":
        return await _admit_trigger(pointer["ownerId"], pointer["triggerId"])
    return await _admit_message(pointer["roomId"], pointer["throughSeq"])


async def _signal(ref: TriggerRef) -> None:
    try:
        await temporal.signal_router(ref)
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise WorkRetry(type(exc).__name__) from exc


async def _to_task(ws: str, task_id: str, trigger_id: str) -> None:
    try:
        await temporal.signal_task_input(ws, task_id, TaskInputRef(trigger_id, "message"))
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise WorkRetry(type(exc).__name__) from exc


async def paused_room(room_id: str | None) -> dict | None:
    """The room's META when it is a paused project (UX slice 6: no new work starts there by any path), else None."""
    meta = await messaging_repo.get_room(room_id) if room_id else None
    return meta if meta and meta.get("pausedAt") else None


async def answer_paused_ask(room_id: str, resumed_by: str | None = None) -> None:
    """Resume (W18-8): the resumer's own latest ask from the pause window, else the latest anyone's, gets its Router
    turn now. Its trigger was never written, so this is its first admission. Never for someone who has left the room
    since. Best effort: a lost answer never fails Resume."""
    ask = await messaging_repo.pop_paused_ask(room_id, resumed_by)
    if ask is None or not ask.get("authorId"):
        return
    if await messaging_repo.get_member(room_id, "human", ask["authorId"]) is None:
        return  # left (or was removed) while paused: no answer to a room they can't read
    try:
        await _admit_message(room_id, ask["seq"])
    except Exception:  # noqa: BLE001 -- Temporal down (WorkRetry) or a deleted message: the person asks again
        logger.warning("paused ask not answered room=%s seq=%s", room_id, ask["seq"], exc_info=True)


async def _to_people(meta: dict, msg: dict) -> bool:
    """W18-7: a message that opens with a person's @handle ("@priya thanks") is people talking (Jev's
    `addressed_to_people`): no Jev call and no Router turn, so the sender's "reading…" ends on the read receipt.
    Members are read only when the text opens with "@" and mentions a person."""
    text = str(msg.get("text") or "").lstrip()
    if not text.startswith("@") or not any(r.get("kind") == "human" for r in msg.get("mentions") or []):
        return False
    members = await messaging_repo.list_members(meta["roomId"])
    return jev.addressed_to_people(msg, messaging.with_human_handles(members, await messaging.human_emails(members)))


async def _admit_trigger(ws: str, trigger_id: str) -> None:
    trig = await tasks_repo.get_trigger(ws, trigger_id)
    if trig is None:
        put_metric("admission.outcome", dimensions={"outcome": "dropped", "source": "trigger"})
        return
    # Voice starts, A2A, proactive ideas, schedules: nothing new starts in a paused project.
    if await paused_room(ts.trigger_room(trig)):
        put_metric("admission.outcome", dimensions={"outcome": "paused", "source": trig["source"]})
        return
    await _signal(TriggerRef(ws, trigger_id, trig["lane"], trig["admission"], ts.trigger_scope(trig)))
    put_metric("admission.outcome", dimensions={"outcome": trig["admission"], "source": trig["source"]})


async def _admit_message(room_id: str, seq: int) -> None:
    meta = await messaging_repo.get_room(room_id)
    msg = await messaging_repo.get_message(room_id, seq)
    if meta is None or msg is None or msg.get("deletedAt"):
        return
    ws = meta["ownerId"]
    await room_memory.ingest_message(ws, meta, msg)  # memory (M4): idempotent, never raises
    author = msg.get("author") or {}
    if author.get("kind") == "system" and msg.get("actorUserId"):
        # A computer's (CLI), the hosted MCP's or A2A's send for a signed-in person is that person's, as the
        # legacy WORK#MESSAGE path woke it. Agent posts and system notices (no accountable person) stay dropped.
        author = {"kind": "human", "id": msg["actorUserId"]}
    if author.get("kind") != "human" or not await router_room(meta, ws):
        put_metric("admission.outcome", dimensions={"outcome": "dropped", "source": "chat"})
        return
    trigger_id = ts.trigger_id("chat", msg["messageId"])
    ref = {"roomId": room_id, "messageId": msg["messageId"]}
    if msg.get("threadRootMessageId"):
        ref["threadRoot"] = msg["threadRootMessageId"]
    if meta.get("meeting"):
        ref["origin"] = "meeting"  # anyone in a Meet acts as the inviter: every spend, send or publish gets a card
    if msg.get("via") == "voice":
        # DeepSeek §4.2, channel-appropriate replies: the Router's and a Worker's input (`activities._input_text`
        # reads `ref.text` first) says the line was spoken, so the answer the voice says back is short
        ref["text"] = f"{msg.get('text') or ''}\n\n{VOICE_HINT}"
    base = {
        "triggerId": trigger_id,
        "source": "chat",
        "actorUserId": author["id"],
        "lane": lane_for(meta, author),
        "receivedAt": utc_now_iso(),
    }

    # 2. pass-through: the task's own room, or a thread a task subscribes to
    task_id = (meta.get("task") or {}).get("taskId")
    if not task_id and msg.get("threadRootMessageId"):
        task_id = await tasks_repo.task_for_thread(ws, f"room:{room_id}:{msg['threadRootMessageId']}")
    if task_id and not await _can_view(ws, task_id, author["id"]):
        task_id = None  # a reply in a private task's thread by someone else: route it like any message
    if task_id:
        trig = {**base, "ref": ref, "admission": "respond", "routedTo": {"taskId": task_id}}
        await tasks_repo.put_trigger(ws, trig)
        if blocked := await task_conversation.gate_input(ws, task_id, trig):  # §14.10: a steer or a new attempt
            await task_conversation.post_as_lightbulb(
                ws,
                room_id,
                text=blocked,
                event_id=f"task-gate:{task_id}:{trigger_id}",
                thread_root=ref.get("threadRoot"),
                remember=False,  # billing copy, not something the room said
            )
            put_metric("admission.outcome", dimensions={"outcome": "gated", "source": "chat"})
            return
        await _to_task(ws, task_id, trigger_id)
        put_metric("admission.outcome", dimensions={"outcome": "pass_through", "source": "chat"})
        return

    # 3. fast path
    front = await _front_member(room_id, ws)
    listen = (front or {}).get("listenMode") or "active"
    addressed = [t["agentId"] for t in msg.get("dispatchTargets") or [] if t.get("eventKind") == "user_request"]
    # "Hi Lightbulb, ..." without the @: speaking to Lightbulb by name addresses it, like an @mention of its seat.
    named = bool(_LIGHTBULB_NAME.search(msg.get("text") or ""))

    # Paused (W17-2): Lightbulb stops, people keep talking. No Router turn and no line in the room (W18-8: the
    # paused bar tells the sender, nobody else); the latest message that spoke to Lightbulb is answered on Resume.
    # Running tasks were passed through above.
    if meta.get("pausedAt"):
        if addressed or named or meta.get("kind") == "dm":
            await messaging_repo.put_paused_ask(room_id, int(msg["messageSeq"]), author["id"])
        put_metric("admission.outcome", dimensions={"outcome": "paused", "source": "chat"})
        return
    pinned = next((a for a in addressed if not front or a != front["id"]), None)
    if pinned:
        ref["pinnedSpecialist"] = pinned
    routed: dict[str, str] | None = None
    if listen == "observe":
        admission = None
    elif addressed or named or meta.get("kind") == "dm" or meta.get("meeting"):
        admission = "respond"  # a meeting's lines are posted only when they named the assistant (meet.py's gate)
    elif listen == "mentions":
        admission = None
    elif await _to_people(meta, msg):
        admission = None
    else:
        # 4. slow path: one Jev call for an unaddressed message
        admission, routed = await _ask_jev(meta, msg, ws)
    if admission is None:
        if listen == "active":  # the person's "Lightbulb is reading…" ends now, not after 60 s of nothing (walk 15)
            await task_conversation.read_quietly(ws, ref)
        put_metric("admission.outcome", dimensions={"outcome": "ignored", "source": "chat"})
        return
    trig = {**base, "ref": ref, "admission": admission}
    if routed:
        trig["routedTo"] = routed
    await tasks_repo.put_trigger(ws, trig)  # a redelivery replays the stored row
    await _signal(TriggerRef(ws, trigger_id, base["lane"], admission, room_id))
    put_metric("admission.outcome", dimensions={"outcome": admission, "source": "chat"})


async def _can_view(ws: str, task_id: str, user_id: str) -> bool:
    task = await tasks_repo.get_task(ws, task_id)
    return task is not None and await tasks_access.can_view(task, user_id)


async def _front_member(room_id: str, ws: str) -> dict | None:
    try:
        return await messaging_repo.get_member(room_id, "agent", await task_conversation.front_agent_id(ws))
    except task_conversation.NoFrontAgent:
        return None


async def _ask_jev(meta: dict, msg: dict, ws: str) -> tuple[str | None, dict | None]:
    from core.repositories import agent_registry_repo

    members = await messaging_repo.list_members(meta["roomId"])
    history = await messaging_repo.list_messages_before(meta["roomId"], int(msg["messageSeq"]), _HISTORY)
    # §14.7: `AGENT#` listenMode active = a Jev candidate; observe never runs.
    listening = {
        a["agentId"]: a.get("name") or a["agentId"]
        for a in await agent_registry_repo.list_agents(ws)
        if a.get("status", "active") == "active" and a.get("listenMode", "active") == "active"
    }
    # §18: the room's proactive rules ride the same call as one extra question (no second Jev call).
    in_scope = rules.rules_for_room(await automations_repo.list_rules(ws), meta["roomId"])
    verdict = await jev.route_for_router(
        meta, msg, members, history, listening, **({"rules": in_scope} if in_scope else {})
    )
    if in_scope:
        if "rule" in verdict:  # parsed even when `who` was unusable
            await _record_rule(ws, meta["roomId"], msg, in_scope, verdict)
        else:
            put_metric("rules.match", dimensions={"outcome": "jev_unavailable"})
    if verdict.get("fallback"):
        put_metric("admission.jev", dimensions={"outcome": "fallback"})
        return None, None  # §14.7: Jev down ⇒ addressed messages only
    if verdict["to"] != "router":
        return None, None
    routed = {"specialist": verdict["specialist"]} if verdict.get("specialist") else None
    return "candidate", routed


async def _record_rule(ws: str, room_id: str, msg: dict, in_scope: list[dict], verdict: dict) -> None:
    """Observe (contracts §18, spec 12 §3.3-3.4): a confident rule pick becomes one "would have done" record.
    Never a task, a Router signal or a push, and never a change to how this message is admitted. The author is
    human and the message unaddressed here (the fast path handled the rest); a task holding the thread already
    passed through above, so the thread gates reduce to "a person stopped work on it"."""
    from core.temporal import rules_activities

    sid = verdict.get("rule")
    rule = next((r for r in in_scope if r["scheduleId"] == sid), None) if sid else None
    if rule is None:
        put_metric("rules.match", dimensions={"outcome": "no_match"})
        return
    thread_key = f"room:{room_id}:{msg.get('threadRootMessageId') or msg['messageId']}"
    gate = rules.code_gates(
        author_kind="human",
        addressed=False,
        thread_task_id=None,
        thread_stopped_by_human=await _stopped_by_human(ws, thread_key),
    )
    if gate:
        put_metric("rules.match", dimensions={"outcome": gate})
        return
    ref = {"roomId": room_id, "messageId": msg["messageId"]}
    if msg.get("threadRootMessageId"):
        ref["threadRoot"] = msg["threadRootMessageId"]
    would = rules.would_do(rule, {"text": messaging_repo.agent_text(msg.get("text") or "")}, rule.get("tz") or "UTC")
    try:
        await rules_activities.record_match(
            ws, sid, rule.get("ruleVersion", 1), thread_key, ref, verdict.get("ruleP"), would, "chat"
        )
    except Exception:  # noqa: BLE001 -- a lost record (version conflict, DynamoDB) never fails admission
        logger.warning("rules: record_match failed ws=%s rule=%s", ws, sid, exc_info=True)


async def _stopped_by_human(ws: str, thread_key: str) -> bool:
    """A thread whose task a person stopped (resolved, reason `stopped`): a rule never picks it up again."""
    thread = await tasks_repo._get(ws, ts.thread_sk(thread_key))
    if not thread or thread.get("active"):
        return False
    task = await tasks_repo.get_task(ws, thread["taskId"])
    return bool(task and task.get("statusReason") == "stopped")
