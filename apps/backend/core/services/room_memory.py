"""Room ingestion: the one writer of room text into workspace memory (spec 07 §3.1, contracts-v2 §12).

Called by the admission consumer once per committed message in a super-agent workspace. It writes one
tagged ``CreateEvent`` (the claim extractor's input) and one turn record (recall's ``Turns:``), scoped by
who may read the room:

- a channel, or a shared task's room → the team actor ``<o>``, ``/turns/<o>/``
- a person's DM with an agent, or a private task's room → ``<o>:<u>``, ``/turns/<o>:<u>/``
- a DM between people, a group DM, an A2A dispatch, a progress line → nothing

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
from core.services.super_agent_flags import flags_for

logger = logging.getLogger(__name__)

_MAX_TEXT = 100_000  # CreateEvent's conversational text limit


def _lane(room: dict, members: list[dict], task: dict | None) -> tuple[str, str | None] | None:
    """``(lane, person)`` for the room, or None when nothing may be stored."""
    if room.get("task"):  # a task's hidden room follows the task's lane
        if task is None:
            return None
        if task.get("lane") == "private":
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


async def ingest_message(owner_id: str, room: dict, message: dict, *, task: dict | None = None) -> bool:
    """Store one committed room message in the lane its room allows. ``room`` is the room's META row,
    ``message`` its MSG row, ``task`` the task row when the message's thread belongs to one (a task room's
    own task is read here when not given). True when written (or already written)."""
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id or _skip(message):
        return False
    try:
        if not await flags_for(owner_id):
            return False
        room_id = room["roomId"]
        if task is None and room.get("task"):
            task = await tasks_repo.get_task(owner_id, room["task"]["taskId"])
        members = room.get("members") or await messaging_repo.list_members(room_id)
        lane = _lane(room, members, task)
        if lane is None:
            return False
        kind, person = lane
        names = workspace_memory.namespaces_for(owner_id, person)
        actor, ns = (f"{owner_id}:{person}", names["private_turns"]) if person else (owner_id, names["turns"])
        root = message.get("threadRootMessageId") or message["messageId"]
        author = message.get("author") or {}
        text = f"{author.get('displayName') or 'Someone'}: {secret_shaped.redact(message['text'])}"[:_MAX_TEXT]
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
