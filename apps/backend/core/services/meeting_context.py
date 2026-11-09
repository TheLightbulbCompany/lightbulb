"""Scoped, read-only source pages for a Router meeting-completion input.

The immutable stored notes request supplies the original principal, room, occurrence and
attachment. Model arguments supply only a trigger ID, section and bounded cursor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import jwt

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services import clerk_admin, messaging
from core.services.service_token import _signing_key

PAGE_CHARS = 8192
TASK_PAGE = 10
GRANT_AUDIENCE = "lightbulb:meeting-context"
GRANT_PURPOSE = "router_meeting_context"
GRANT_TTL = 600  # bounded cold-start + the Router's existing four-minute model budget
GRANT_HEADER = "X-Lightbulb-Meeting-Context-Grant"
MAX_TRIGGERS = 20


class NotAuthorized(ValueError):
    pass


def mint_grant(owner_id: str, run_id: str, trigger_ids: list[str]) -> str:
    """Backend-only capability for the actual current batch; never a model argument."""
    if (
        not trigger_ids
        or len(trigger_ids) > MAX_TRIGGERS
        or not all(isinstance(t, str) and 0 < len(t) <= 200 for t in trigger_ids)
    ):
        raise ValueError("invalid_meeting_context_batch")
    now = int(time.time())
    return jwt.encode(
        {
            "kind": GRANT_PURPOSE,
            "aud": GRANT_AUDIENCE,
            "ownerId": owner_id,
            "runId": run_id,
            "triggerIds": list(dict.fromkeys(trigger_ids)),
            "iat": now,
            "exp": now + GRANT_TTL,
            "jti": str(uuid.uuid4()),
        },
        _signing_key(),
        algorithm="HS256",
    )


def verify_grant(token: str | None, owner_id: str, run_id: str, trigger_id: str) -> None:
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise ValueError
        claims = jwt.decode(token, _signing_key(), algorithms=["HS256"], audience=GRANT_AUDIENCE)
        ids = claims.get("triggerIds")
        if (
            set(claims) != {"kind", "aud", "ownerId", "runId", "triggerIds", "iat", "exp", "jti"}
            or claims["kind"] != GRANT_PURPOSE
            or claims["ownerId"] != owner_id
            or claims["runId"] != run_id
            or not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_TRIGGERS
            or trigger_id not in ids
            or not all(isinstance(t, str) and 0 < len(t) <= 200 for t in ids)
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not claims["iat"] <= int(time.time()) < claims["exp"] <= claims["iat"] + GRANT_TTL
        ):
            raise ValueError
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, RuntimeError):
        raise NotAuthorized("meeting_context_grant_refused") from None


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


async def context(
    owner_id: str, run_id: str, trigger_id: str, section: str, cursor: str | None = None, *, grant: str | None
) -> dict:
    """Bounded pages from the original artifact/tasks; cursor data is never an authorization source."""
    from core.services.meeting_notes import _meeting_tasks

    verify_grant(grant, owner_id, run_id, trigger_id)
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
