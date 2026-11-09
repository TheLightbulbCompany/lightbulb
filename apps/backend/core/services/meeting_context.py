"""Scoped, read-only source pages for a Router meeting-completion input.

The immutable stored notes request supplies the original principal, room, occurrence and
attachment. Model arguments supply only a trigger ID, section and bounded cursor.
"""

from __future__ import annotations

import base64
import hashlib
import json

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services import clerk_admin, messaging

PAGE_CHARS = 8192
TASK_PAGE = 10


class NotAuthorized(ValueError):
    pass


async def enabled(owner_id: str) -> bool:
    config = await messaging_repo.get_voice_agent(owner_id) or {}
    meetings = config.get("meetings", {})
    # Match the product's default-on checkbox. Explicit false, or malformed config, is record-only.
    return isinstance(meetings, dict) and meetings.get("followUps", True) is True


def occurrence_entry(meeting: dict, occurrence) -> dict:
    if meeting.get("startAt") == occurrence:
        return meeting
    return next((h for h in meeting.get("history") or [] if h.get("startAt") == occurrence), {})


async def _authorize(owner_id: str, trigger: dict, request: dict | None) -> dict:
    ref = trigger.get("ref") or {}
    completion = ref.get("meetingFollowups") or {}
    principal = (request or {}).get("principalUserId")
    snapshot = (request or {}).get("meeting") or {}
    if not isinstance(completion, dict) or not isinstance(snapshot, dict):
        raise NotAuthorized("not_meeting_completion")
    if (
        not request
        or not principal
        or trigger.get("actorUserId") != principal
        or completion.get("occurrence") != request.get("occurrence")
        or ref.get("roomId") != request.get("roomId")
        or ref.get("origin") != "meeting"
        or not snapshot.get("transcriptId")
        or not await enabled(owner_id)
    ):
        raise NotAuthorized("meeting_followups_not_authorized")
    room = await messaging_repo.get_room(request["roomId"]) or {}
    entry = occurrence_entry(room.get("meeting") or {}, request["occurrence"])
    if room.get("ownerId") != owner_id or entry.get("transcriptId") != snapshot["transcriptId"]:
        raise NotAuthorized("not_original_meeting_occurrence")
    member = await messaging_repo.get_member(request["roomId"], "human", principal)
    if not member or member.get("id") != principal or member.get("kind") != "human" or member.get("role") != "owner":
        raise NotAuthorized("original_inviter_no_longer_authorized")
    if owner_id.startswith("org_"):
        members = await clerk_admin.list_organization_members(owner_id, strict=True)
        if not any(m.get("user_id") == principal for m in members):
            raise NotAuthorized("original_inviter_left_workspace")
    elif owner_id != principal:
        raise NotAuthorized("not_workspace_owner")
    return request


async def authorize(owner_id: str, trigger: dict) -> dict:
    completion = (trigger.get("ref") or {}).get("meetingFollowups") or {}
    request_id = completion.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise NotAuthorized("not_meeting_completion")
    request = await messaging_repo.get_meeting_notes_request(owner_id, request_id)
    return await _authorize(owner_id, trigger, request)


def _cursor(value: str | None, artifact: str, section: str, length: int) -> int:
    if value is None:
        return 0
    try:
        if len(value) > 512:
            raise ValueError
        raw = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        offset = raw["o"]
        if (
            set(raw) != {"a", "s", "o"}
            or raw["a"] != artifact
            or raw["s"] != section
            or type(offset) is not int
            or not 0 <= offset <= length
        ):
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_meeting_context_cursor") from exc


def _next(artifact: str, section: str, offset: int, length: int) -> str | None:
    if offset >= length:
        return None
    return (
        base64.urlsafe_b64encode(json.dumps({"a": artifact, "s": section, "o": offset}).encode()).decode().rstrip("=")
    )


async def context(owner_id: str, trigger_id: str, section: str, cursor: str | None = None) -> dict:
    """Bounded pages from the original artifact/tasks; cursor data is never an authorization source."""
    from core.services.meeting_notes import _meeting_tasks

    if section not in {"transcript", "tasks"}:
        raise ValueError("invalid_meeting_context_section")
    trigger = await tasks_repo.get_trigger(owner_id, trigger_id)
    if not trigger:
        raise NotAuthorized("not_meeting_completion")
    request = await authorize(owner_id, trigger)
    snapshot = request["meeting"]
    attachment_id = snapshot["transcriptId"]
    artifact = hashlib.sha256(
        json.dumps([owner_id, request["roomId"], request["occurrence"], attachment_id]).encode()
    ).hexdigest()
    if section == "transcript":
        _, raw = await run_in_thread(
            messaging.load_attachment, owner_id, snapshot.get("transcriptRoomId") or request["roomId"], attachment_id
        )
        text = raw.decode("utf-8", "replace")
        offset = _cursor(cursor, artifact, section, len(text))
        end = min(offset + PAGE_CHARS, len(text))
        return {
            "section": section,
            "quotedSource": True,
            "text": text[offset:end],
            "offset": offset,
            "totalCharacters": len(text),
            "truncated": end < len(text),
            "nextCursor": _next(artifact, section, end, len(text)),
            "artifact": {"id": attachment_id, "roomId": snapshot.get("transcriptRoomId") or request["roomId"]},
        }
    tasks = await _meeting_tasks(
        owner_id,
        request["roomId"],
        int(snapshot.get("inCallAt") or 0),
        int(snapshot.get("endedAt") or snapshot.get("endAt") or 0),
    )
    tasks = sorted(tasks, key=lambda t: t["taskId"])
    offset = _cursor(cursor, artifact, section, len(tasks))
    end = min(offset + TASK_PAGE, len(tasks))
    return {
        "section": section,
        "tasks": [
            {
                "taskId": t["taskId"],
                "title": str(t.get("title") or "")[:200],
                "description": str(t.get("description") or "")[:500],
                "status": t.get("status"),
            }
            for t in tasks[offset:end]
        ],
        "totalTasks": len(tasks),
        "truncated": end < len(tasks),
        "nextCursor": _next(artifact, section, end, len(tasks)),
    }
