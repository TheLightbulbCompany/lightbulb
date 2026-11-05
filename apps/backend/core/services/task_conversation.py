"""Where a task's conversation lives, and the one voice that posts for tasks (contracts-v2 §6, §9; G2).

Every task gets a hidden room (`ts.task_room_id`), the same pattern as an app's conversation
(`app_conversation`): deterministic id, created on first need, readers seated when they open it (after
`tasks_access.can_view`). Only Lightbulb (the front agent: the workspace's CEO agent, shown "Lightbulb ·
<specialist>") posts in rooms people chat in; results, acks and cards go through `post_as_lightbulb`.
"""

from __future__ import annotations

import uuid

from core import tasks_schema as ts
from core.constants import SYSTEM_ACTOR_ID
from core.repositories import messaging_repo
from core.services import messaging, session_chat

_RESULT_SCAN = 50


class NoFrontAgent(RuntimeError):
    """The workspace has no active CEO agent to speak as Lightbulb."""


async def _front_agent(ws: str) -> dict:
    agents = await session_chat._default_agents(ws)
    if not agents:
        raise NoFrontAgent(ws)
    return agents[0]


async def front_agent_id(ws: str) -> str:
    """The CEO agent id: the identity every Lightbulb post uses (displayed "Lightbulb" / `@lightbulb`)."""
    return (await _front_agent(ws))["id"]


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
                extra={"hidden": True, "task": {"taskId": task["taskId"]}},
            )
        except messaging_repo.MessagingRepoError:
            meta = await messaging_repo.get_room(room_id)  # a concurrent first open created it
            if meta is None:
                raise
    if seat_viewer and await messaging_repo.get_member(room_id, "human", seat_viewer) is None:
        await session_chat._seat(ws, meta, seat_viewer, full="This task's conversation is full.")
    return room_id


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
) -> str:
    """Post as Lightbulb; idempotent on `event_id`. Returns the message id."""
    receipt = await messaging.create_agent_message(
        ws,
        messaging.AgentMessageRequest(
            event_id=event_id,
            room_id=uuid.UUID(room_id),
            agent_id=uuid.UUID(await front_agent_id(ws)),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=uuid.UUID(thread_root) if thread_root else None,
            progress=progress,
            task_refs=task_refs,
            specialist=specialist,
            kind=kind,
        ),
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
    except messaging_repo.IdempotencyConflict:
        return
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
