"""Super-agent memory tags and the run ``memory`` block (spec 07 §3.2, §5.2; contracts-v2 §12).

The backend computes the block; the bridge and plugin only read it. ``member`` (a private lane's
person) therefore never comes from a model argument (spec 07 L2)."""

import hashlib
import re

from core import tasks_schema as ts

# botocore's metadata value pattern (CreateEvent / BatchCreateMemoryRecords).
_TAG = re.compile(r"[a-zA-Z0-9\s._:/=+@-]{1,128}")
_KEYS = ("taskId", "threadId", "specialist", "lane", "source", "roomId")


def encode_thread(thread_key: str) -> str:
    """The thread key when it is a valid tag value, else ``h:`` + sha256[:40]; the full key stays in the tasks table."""
    if _TAG.fullmatch(thread_key):
        return thread_key
    return "h:" + hashlib.sha256(thread_key.encode()).hexdigest()[:40]


def tags(**kw: str | None) -> dict[str, str]:
    """Event/record metadata: empty and off-pattern values dropped; ``threadId`` encoded."""
    unknown = set(kw) - set(_KEYS)
    if unknown:
        raise TypeError(f"unknown memory tag {sorted(unknown)}")
    if kw.get("threadId"):
        kw["threadId"] = encode_thread(kw["threadId"])
    return {k: v for k, v in kw.items() if v and _TAG.fullmatch(v)}


def _person(user_id: str | None) -> dict:
    if not user_id:
        raise ValueError("a private lane needs its person")
    return {"kind": "person", "userId": user_id}


def block_for_worker(task: dict) -> dict:
    """A Worker turn on ``task`` (a tasks-table row): it writes tagged events in the task's lane."""
    private = task.get("lane") == "private"
    audience = task.get("audience") or {}
    if private:
        shown = _person(task.get("ownerUserId"))
    elif audience.get("kind") == "room":
        if not audience.get("roomId"):
            raise ValueError("a room audience names its room")
        shown = {"kind": "room", "roomId": audience["roomId"]}
    else:
        shown = {"kind": "workspace"}
    return {
        "lane": "private" if private else "shared",
        "member": task["ownerUserId"] if private else None,
        "audience": shown,
        "tags": tags(
            taskId=task["taskId"],
            threadId=ts.task_room_id(task["taskId"]),
            specialist=task.get("specialist"),
            source="worker",
        ),
        "sync": "events",
    }


def block_for_router(lane: str, user_id: str | None, room_id: str | None) -> dict:
    """A Router turn: it writes nothing (room ingestion is the one writer of room text)."""
    private = lane == "private"
    if private:
        audience = _person(user_id)
    else:  # no reply room (a schedule, mail): links only to workspace-wide tasks
        audience = {"kind": "room", "roomId": room_id} if room_id else {"kind": "workspace"}
    return {
        "lane": "private" if private else "shared",
        "member": user_id if private else None,
        "audience": audience,
        "tags": {},
        "sync": "none",
    }
