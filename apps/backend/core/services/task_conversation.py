"""Where a task's conversation lives, and the one voice that posts for tasks (contracts-v2 §6, §9; G2).

Every task gets a hidden room (`ts.task_room_id`), the same pattern as an app's conversation
(`app_conversation`): deterministic id, created on first need, readers seated when they open it (after
`tasks_access.can_view`). Only Lightbulb (the front agent: the workspace's CEO agent, shown "Lightbulb ·
<specialist>") posts in rooms people chat in; results, acks and cards go through `post_as_lightbulb`.
"""

from __future__ import annotations

import logging
import uuid

from core import tasks_schema as ts
from core.constants import SYSTEM_ACTOR_ID
from core.repositories import messaging_repo
from core.services import agent_registry, messaging, room_memory, session_chat

_RESULT_SCAN = 50
logger = logging.getLogger(__name__)


class NoFrontAgent(RuntimeError):
    """The workspace has no available designated agent to speak as Lightbulb."""


async def _front_agent(ws: str, *, room_id: str | None = None) -> dict:
    """One existing worker speaks for the workspace; selection never renames or recreates it.

    An explicit unavailable selection fails closed, so another agent cannot silently take its place.
    Fresh rosters use the persisted CEO UUID; legacy rosters retain their existing root mechanism.
    """
    from core.repositories.workspace_repo import WorkspaceRepo

    if room_id:
        meta = await messaging_repo.get_room(room_id)
        if meta is None or meta.get("ownerId") != ws or meta.get("deletedAt"):
            raise NoFrontAgent("Conversation unavailable.")
        pinned = meta.get("frontAgentId")
        # New rooms pin the server-selected UUID. Older rooms retain their server-reserved seat.
        room_members = await messaging_repo.list_members(room_id)
        seats = [
            m
            for m in room_members
            if m.get("kind") == "agent"
            and m.get("role") == "member"
            and (m.get("id") == pinned if pinned else m.get("mentionHandle") == "lightbulb")
            and messaging_repo.member_home(m, ws) == ws
        ]
        if len(seats) > 1:
            raise NoFrontAgent("This conversation has conflicting front-door seats.")
        if pinned and not seats:
            raise NoFrontAgent("This conversation's front-door agent is no longer a member.")
        if seats:
            roster = await agent_registry.roster(ws)
            if any(a["id"] == seats[0]["id"] and a.get("status") == "active" for a in roster):
                return seats[0]
            raise NoFrontAgent(
                "This conversation's agent is unavailable. Start a new conversation with an active front-door agent."
            )
    config = await messaging_repo.get_voice_agent(ws) or {}
    roster = await agent_registry.roster(ws)
    selected = config.get("frontAgentId")
    company = await WorkspaceRepo().resolve_owner_row(ws)
    stable = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    target = selected or stable
    if target:
        agent = next((a for a in roster if a["id"] == target and a.get("status") == "active"), None)
        if agent is None:
            raise NoFrontAgent(
                "The selected front-door agent is unavailable. Choose an active agent in Agent settings."
            )
    else:
        agent = next(
            (a for a in roster if a.get("role") == "ceo" and not a.get("reportsTo") and a.get("status") == "active"),
            None,
        )
        if agent is None:
            agents = await session_chat._default_agents(ws)
            if not agents:
                raise NoFrontAgent(ws)
            agent = agents[0]
    if room_id and not any(
        m.get("kind") == "agent"
        and m.get("role") == "member"
        and m.get("id") == agent["id"]
        and messaging_repo.member_home(m, ws) == ws
        for m in room_members
    ):
        raise NoFrontAgent("This conversation has no authorized front-door member.")
    return {
        "kind": "agent",
        "id": agent["id"],
        "displayName": messaging.FRONT_AGENT_NAME,
        "mentionHandle": "lightbulb",
        "role": "member",
        "listenMode": "mentions",
    }


async def front_agent_id(ws: str, *, room_id: str | None = None) -> str:
    """The designated existing agent id: the identity every Lightbulb post uses (displayed "Lightbulb" / `@lightbulb`)."""
    return (await _front_agent(ws, room_id=room_id))["id"]


async def read_quietly(ws: str, ref: dict) -> None:
    """Lightbulb's read receipt on a chat message it handled without posting (admission or the Router let it pass,
    or a task took it with no ack), so the person's "Lightbulb is reading…" line ends now instead of on its timer
    (dev walks 13, 15). Best-effort: a lost receipt leaves the client's timeout."""
    if not ref.get("roomId") or not ref.get("messageId"):
        return
    try:
        agent_id = await front_agent_id(ws, room_id=ref["roomId"])
        await messaging._publish_room_event(
            ref["roomId"],
            messaging.make_live_event(
                "room.read.changed",
                {"roomId": ref["roomId"], "reader": {"kind": "agent", "id": agent_id}, "messageId": ref["messageId"]},
                room_id=ref["roomId"],
            ),
        )
    except Exception:  # noqa: BLE001 -- a receipt never fails its caller
        logger.warning("read receipt failed for message %s", ref.get("messageId"), exc_info=True)


async def ensure_task_room(ws: str, task: dict, *, seat_viewer: str | None = None) -> str:
    """The task's hidden room, created on first need with the front agent and a person task's audience.
    `seat_viewer` seats a reader; callers check `tasks_access.can_view` first."""
    room_id = ts.task_room_id(task["taskId"])
    meta = await messaging_repo.get_room(room_id)
    if meta is None:
        audience = task.get("audience") or {}
        humans = (audience.get("userIds") or []) if audience.get("kind") == "person" else []
        members = [await _front_agent(ws)] + [
            {
                "kind": "human",
                "id": uid,
                "displayName": await messaging.human_display_name(uid),
                "mentionHandle": None,
                "role": "member",
                "listenMode": None,
            }
            for uid in humans
        ]
        try:
            meta = await messaging_repo.create_room(
                owner_id=ws,
                room_id=room_id,
                client_room_id=f"task-conversation-v1:{task['taskId']}",
                creator_user_id=SYSTEM_ACTOR_ID,
                name=f"Task · {task.get('title') or 'Task'}"[:200],
                kind="channel",
                members=members,
                req_hash=messaging_repo.request_hash("task-conversation-v1", task["taskId"]),
                extra={"hidden": True, "task": {"taskId": task["taskId"]}, "frontAgentId": members[0]["id"]},
            )
        except messaging_repo.MessagingRepoError:
            meta = await messaging_repo.get_room(room_id)  # a concurrent first open created it
            if meta is None:
                raise
    if seat_viewer and await messaging_repo.get_member(room_id, "human", seat_viewer) is None:
        await session_chat._seat(ws, meta, seat_viewer, full="This task's conversation is full.")
    return room_id


async def gate_input(ws: str, task_id: str, trig: dict) -> str | None:
    """§14.10 for a person's input to a task (a steer, or the attempt it starts): the gate's copy when
    blocked, else None. Inputs with no person behind them (a delegation result) are not gated. A line from a Meet
    (`ref.origin: meeting`, anyone there speaks as the inviter) first makes the task `confirmAll`: from then on every
    spend, send or publish it makes gets a card, as for a task the meeting started."""
    from core.services import chat_gate

    if (trig.get("ref") or {}).get("origin") == "meeting":
        await _confirm_all(ws, task_id)
    actor = trig.get("actorUserId")
    if not actor:
        return None
    gate = await chat_gate.gate_chat(user_id=actor, owner_id=ws, session_key=f"task:{task_id}")
    return gate["message"] if gate.get("blocked") else None


async def _confirm_all(ws: str, task_id: str) -> None:
    from core.repositories import tasks_repo

    for _ in range(5):
        task = await tasks_repo.get_task(ws, task_id)
        if task is None or task.get("confirmAll"):
            return
        try:
            await tasks_repo.update_task_fields(ws, task_id, {"confirmAll": True}, expected_version=task["version"])
            return
        except tasks_repo.VersionConflict:
            continue  # the workflow moved the row: read it again
    raise RuntimeError(f"task {task_id}: confirmAll not set")  # fail closed: the input is retried, never let through


async def post_as_lightbulb(
    ws: str,
    room_id: str,
    *,
    text: str,
    event_id: str,
    specialist: str | None = None,
    thread_root: str | None = None,
    task_refs: list[dict] | None = None,
    kind: str | None = None,
    progress: bool = False,
    attrs: dict | None = None,
    remember: bool = True,
    uploaded_attachments: list[dict] | None = None,
    in_reply_to: str | None = None,
) -> str:
    """Post as Lightbulb; idempotent on `event_id`. Returns the message id. `attrs`: extra message attributes
    (lane A's confirm card carries `automationId`). `in_reply_to`: the human message it answers. The post goes into room memory like a person's message
    (agent posts write no admit row, so admission never ingests them); progress lines, question cards (the
    Question row holds them) and `remember=False` (the Router's ack-only or fallback posts) stay out."""
    if kind is None:  # Lane Q: never claim an approval a Question row doesn't show (spec 05 §3.5 layer 3)
        from core.services import questions

        if (hit := await questions.guard_reply(ws, text, task_refs, specialist)) is not None:
            text, task_id, qid = hit
            kind = "question"  # one card, bound to {taskId, qid}: the client renders its buttons once
            task_refs = [{**r, "qid": qid} if r.get("taskId") == task_id else r for r in task_refs or []]
    agent_id = await front_agent_id(ws, room_id=room_id)
    receipt = await messaging.create_agent_message(
        ws,
        messaging.AgentMessageRequest(
            event_id=event_id,
            room_id=uuid.UUID(room_id),
            agent_id=uuid.UUID(agent_id),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=uuid.UUID(thread_root) if thread_root else None,
            progress=progress,
            uploaded_attachments=[messaging.UploadedAttachment.model_validate(a) for a in uploaded_attachments or []],
        ),
        attrs={
            **(attrs or {}),
            **{k: v for k, v in (("taskRefs", task_refs), ("specialist", specialist), ("kind", kind)) if v is not None},
        },
        **({"in_reply_to": in_reply_to} if in_reply_to else {}),
    )
    if remember and not progress and kind != "question":  # a question card is already the Question row
        await room_memory.ingest_posted(
            ws,
            room_id,
            {
                "messageId": str(receipt.message_id),
                "text": text,
                "author": {"kind": "agent", "id": agent_id, "displayName": "Lightbulb"},
                "threadRootMessageId": thread_root,  # no createdAt: ingest stamps now, which is when it was posted
            },
        )
    return str(receipt.message_id)


async def forward_card_reply(ws: str, task_id: str, message: dict) -> None:
    """A reply to a task card in the origin room becomes one line in the task room (once per message)."""
    room_id = ts.task_room_id(task_id)
    key = f"task-forward:{message['messageId']}"
    who = (message.get("author") or {}).get("displayName") or "Someone"
    try:
        item, replayed = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=ws,
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=messaging_repo.human_idemp_sk(SYSTEM_ACTOR_ID, key),
            req_hash=messaging_repo.request_hash(key),
            author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
            text=f"**{who}** replied on the card: {message.get('text', '')}",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
            attrs={"forwardedFrom": {"roomId": message["roomId"], "messageId": message["messageId"]}},
        )
    except (messaging_repo.IdempotencyConflict, messaging_repo.RoomNotFound):
        return  # already forwarded, or the task's room is not there yet (nothing to catch up on)
    if not replayed:
        messaging._publish_message_created_soon(item)


async def last_result_text(ws: str, task_id: str) -> str | None:
    """The text of the task's newest result line (`kind="result"`), for the next run of an automation."""
    room_id = ts.task_room_id(task_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None:
        return None
    rows = await messaging_repo.list_messages_before(room_id, int(meta.get("lastSeq") or 0) + 1, _RESULT_SCAN)
    for row in reversed(rows):
        if row.get("kind") == "result" and not row.get("deletedAt"):
            return row.get("editedText") or row.get("text")
    return None
