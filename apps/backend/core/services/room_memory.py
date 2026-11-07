"""Room ingestion: the one writer of room text into workspace memory (spec 07 §3.1, contracts-v2 §12).

Called by the admission consumer once per committed message in a super-agent workspace. It writes one
tagged ``CreateEvent`` (the claim extractor's input) and one turn record (recall's ``Turns:``), scoped by
who may read the room:

- a channel, or a shared task's room → the team actor ``<o>``, ``/turns/<o>/``
- a person's DM with an agent, or a private or person-visible task's room → ``<o>:<u>``, ``/turns/<o>:<u>/``
- a group chat with Lightbulb, or a shared task's room whose task is that chat's → the chat's own scope
  ``<o>:room_<roomId>`` (founder ruling, 6 Nov 2026: recalled only in that chat, never workspace-wide)
- a DM between people, a group DM without an agent, an A2A dispatch, a progress line → nothing

Idempotent on the message id (SQS redelivers): the event's ``clientToken`` and the turn record's
``requestIdentifier`` both derive from it, and a ``MEMTURN#`` row marks it done. Best effort: a failure
logs its type and returns False, so the message and its routing never wait on memory.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services import memory_sync, memory_tags, secret_shaped, workspace_memory

logger = logging.getLogger(__name__)

_MAX_TEXT = 100_000  # CreateEvent's conversational text limit


def _lane(room: dict, members: list[dict], task: dict | None) -> tuple[str, str | None] | None:
    """``(lane, person)`` for the room, or None when nothing may be stored."""
    if room.get("task"):  # a task's hidden room follows the task's lane
        if task is None:
            return None
        if task.get("lane") == "private" or (task.get("audience") or {}).get("kind") == "person":
            # A person-visible task is that person's wherever it was asked (a DM, a group chat, a channel).
            return ("private", task["ownerUserId"]) if task.get("ownerUserId") else None
        return "shared", None
    kind = room.get("kind")
    if kind == "channel":
        return "shared", None
    if kind == "dm":
        humans = [m["id"] for m in members if m.get("kind") == "human"]
        agents = [m for m in members if m.get("kind") == "agent"]
        if len(humans) == 1 and agents:
            return "private", humans[0]
    return None  # people-only DMs, group DMs, anything else


async def group_room(owner_id: str, room: dict, task: dict | None = None) -> str | None:
    """The group chat whose own memory scope ``room`` writes to and recall reads, else None: a group chat with an agent
    in it, or the hidden room of a shared task whose audience is one."""
    if room.get("task"):
        if task is None:
            task = await tasks_repo.get_task(owner_id, room["task"]["taskId"])
        audience = (task or {}).get("audience") or {}
        # Keyed on the room it came from. A private or person-visible task is its person's (`_lane` → private lane),
        # never a scope the rest of the chat reads.
        if (task or {}).get("lane") == "private" or audience.get("kind") == "person" or not audience.get("roomId"):
            return None
        room = await messaging_repo.get_room(audience["roomId"]) or {}
    if room.get("kind") != "group-dm" or room.get("ownerId") != owner_id:
        return None
    members = room.get("members") or await messaging_repo.list_members(room["roomId"])
    return room["roomId"] if any(m.get("kind") == "agent" for m in members) else None


async def group_room_by_id(owner_id: str, room_id: str | None) -> str | None:
    """`group_room` for a room id (recall's room audience). Never raises: no scope is the safe answer."""
    try:
        room = await messaging_repo.get_room(room_id) if room_id else None
        return await group_room(owner_id, room) if room else None
    except Exception as exc:  # noqa: BLE001 -- recall answers without the chat's scope rather than failing
        logger.warning("group scope lookup failed ws=%s error=%s", owner_id, type(exc).__name__)
        return None


def _skip(message: dict) -> bool:
    author = message.get("author") or {}
    return (
        not (message.get("text") or "").strip()
        or message.get("progress") is True
        or str(author.get("id") or "").startswith("a2a:")  # network/A2A turns stay out (28 Oct ruling)
    )


def _write(
    owner_id: str,
    memory_id: str,
    actor: str,
    ns: str,
    session: str,
    role: str,
    text: str,
    when: datetime,
    meta: dict[str, str],
    message_id: str,
) -> None:
    token = hashlib.sha256(message_id.encode()).hexdigest()
    if memory_sync._get(owner_id, f"MEMTURN#{token}"):
        return  # a redelivery of a message already stored
    client = workspace_memory._client()
    client.create_event(
        memoryId=memory_id,
        actorId=actor,
        sessionId=session,
        eventTimestamp=when,
        payload=[{"conversational": {"content": {"text": text}, "role": role}}],
        metadata={k: {"stringValue": v} for k, v in meta.items()},
        clientToken=token,
    )
    resp = client.batch_create_memory_records(
        memoryId=memory_id,
        clientToken=memory_sync._token([token]),
        records=[
            {
                "requestIdentifier": token,
                "namespaces": [ns],
                "content": {"text": text},
                "timestamp": when,
                "metadata": memory_sync._strings(speaker=text.partition(": ")[0], session=session, **meta),
            }
        ],
    )
    memory_sync._write_ok(resp, "room turn")
    memory_sync._put(owner_id, f"MEMTURN#{token}", expiresAt=int(time.time()) + memory_sync._TURN_TTL_S)


async def ingest_posted(owner_id: str, room_id: str, message: dict) -> bool:
    """Lightbulb's own post (`task_conversation.post_as_lightbulb`, which builds ``message`` from what it wrote),
    stored exactly as a person's message is. Never raises."""
    try:
        if not settings.AGENTCORE_MEMORY_ID:
            return False
        room = await messaging_repo.get_room(room_id)
    except Exception as exc:  # noqa: BLE001 -- memory never blocks a post
        logger.warning(
            "posted ingest failed ws=%s message=%s error=%s", owner_id, message["messageId"], type(exc).__name__
        )
        return False
    return room is not None and await ingest_message(owner_id, room, message)


async def ingest_message(owner_id: str, room: dict, message: dict, *, task: dict | None = None) -> bool:
    """Store one committed room message in the lane its room allows. ``room`` is the room's META row,
    ``message`` its MSG row, ``task`` the task row when the message's thread belongs to one (a task room's
    own task is read here when not given). True when written (or already written)."""
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id or _skip(message):
        return False
    try:
        room_id = room["roomId"]
        if task is None and room.get("task"):
            task = await tasks_repo.get_task(owner_id, room["task"]["taskId"])
        members = room.get("members") or await messaging_repo.list_members(room_id)
        if group := await group_room(owner_id, {**room, "members": members}, task):
            kind, actor = "room", workspace_memory.room_actor(owner_id, group)
            ns = workspace_memory.room_namespaces(owner_id, group)["room_turns"]
        else:
            lane = _lane(room, members, task)
            if lane is None:
                return False
            kind, person = lane
            names = workspace_memory.namespaces_for(owner_id, person)
            actor, ns = (f"{owner_id}:{person}", names["private_turns"]) if person else (owner_id, names["turns"])
        root = message.get("threadRootMessageId") or message["messageId"]
        author = message.get("author") or {}
        clean = messaging_repo.agent_text(message["text"])
        text = f"{author.get('displayName') or 'Someone'}: {secret_shaped.redact(clean)}"[:_MAX_TEXT]
        meta = memory_tags.tags(
            threadId=f"room:{room_id}:{root}",
            roomId=room_id,
            lane=kind,
            source="room",
            taskId=(task or {}).get("taskId"),
        )
        when = datetime.fromisoformat(message["createdAt"]) if message.get("createdAt") else datetime.now(timezone.utc)
        await run_in_thread(
            _write,
            owner_id,
            memory_id,
            actor,
            ns,
            f"room-{room_id}-{message.get('threadRootMessageId') or 'top'}",
            "ASSISTANT" if author.get("kind") == "agent" else "USER",
            text,
            when,
            meta,
            message["messageId"],
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- memory never blocks a message or its routing
        logger.warning(
            "room ingest failed ws=%s message=%s error=%s", owner_id, message.get("messageId"), type(exc).__name__
        )
        return False
