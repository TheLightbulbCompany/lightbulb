"""Resume: start an ended session again on its owner's Mac, from web, desktop or iPhone (spec §5.7).

Who: the session's owner, and the people who could send to it when it ended (``resumeAllowed``,
copied by the relay's end handler; the owner can take it back). Never an agent.

Rows, all in the messaging table:
  LOCALSESSION#<owner>#<sid> / META                       ``resume``: the one waiting request
  LOCALUSER#<owner>#<human>  / RESUMEWANT#<device>#<sid>  what the owner's Mac is asked to start

The waiting request lives on META, not in a row of its own: its one-per-session condition is the
META revision, so it is decided together with "the session has ended" and "this person may
resume", and every reader gets its state from the row it already reads. The want row is the Mac's
copy, delivered in the device-heartbeat acknowledgement like pick-up's, so a Mac that is asleep
starts it when it wakes. Both expire after RESUME_SECONDS (the want row by DynamoDB TTL).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import Field, ValidationError

from core.auth import AuthContext, resolve_owner_id
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.desktop_execution import submissions_enabled
from core.services.local_session_grants import AgentInvocation
from core.services.messaging import ApiProblem, ContractModel, validate_uuid

logger = logging.getLogger(__name__)

RESUME_SECONDS = 24 * 3600


class ResumeRequest(ContractModel):
    # Client-minted: a retry of the same click is the same request, never a second resume.
    resume_id: str
    message: Annotated[str, Field(max_length=sessions.MAX_COMMAND_BYTES)] | None = None


def live(resume: dict | None, now: int | None = None) -> bool:
    """A request still waiting for the Mac. A refused or expired one is history."""
    now = int(time.time()) if now is None else now
    return bool(resume) and resume.get("state") == "pending" and int(resume["until"]) > now


def _want_key(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"RESUMEWANT#{row['deviceId']}#{row['sessionId']}",
    }


def _want_delete(row: dict) -> dict:
    return {"Delete": {"TableName": store._table().name, "Key": _want_key(row)}}


def _mode(row: dict, own: bool) -> Literal["plan", "default"]:
    """Anyone but the owner: plan, as desktop_session_start forces for a remote start. The owner:
    the mode the session last ran in, capped at default."""
    if not own:
        return "plan"
    return "plan" if (row.get("stats") or {}).get("permissionMode") == "plan" else "default"


async def request_resume(
    auth: AuthContext | AgentInvocation, session_id: str, request: ResumeRequest
) -> sessions.LocalSession:
    from core.services.local_session_relay import publish_resources_changed
    from core.services.messaging import human_display_name

    if isinstance(auth, AgentInvocation):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only a person can resume a session.")
    resume_id = validate_uuid(request.resume_id, label="resume ID")
    text = (request.message or "").strip() or None
    owner, me = resolve_owner_id(auth), auth.user_id
    if not submissions_enabled(owner):
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Resuming sessions is not available in this workspace.")
    for _ in range(6):
        # Reading it needs sharing still enrolled on the owner's Mac: a Mac that left cannot run it.
        row, enrollment = await sessions.authorize_session(auth, None, session_id)
        sid, own, waiting = row["sessionId"], me == row["humanUserId"], row.get("resume")
        if live(waiting) and waiting["resumeId"] == resume_id and waiting["byUserId"] == me:
            return sessions._session_public(row, me)  # the same click, retried
        if row.get("endedAt") is None:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "This session is still running.")
        if not own and me not in (row.get("resumeAllowed") or []):
            raise ApiProblem(
                "LOCAL_SESSION_FORBIDDEN",
                "Only the owner and the people who could send to this session when it ended can resume it.",
            )
        if row.get("kind") == "background":
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Background sessions cannot be resumed.")
        if live(waiting):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "Someone already asked to resume this session.")
        if not own:
            stream = await store.get_user_stream(owner, me)
            if not stream or not stream.get("active"):
                raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "You are no longer in this workspace.")
        if text:
            try:
                # The same bound as any follow-up: it becomes one when the new run registers.
                sessions.LocalSessionCommand(
                    requestId=resume_id, expectedGeneration=row["generation"], kind="send", text=text
                )
            except ValidationError as exc:
                raise ApiProblem("INVALID_REQUEST", "That message is too long.") from exc
            # Admitted under this id at registration; an id some command already holds would cancel
            # that registration on every retry.
            if await repo.get_command(owner, sid, resume_id):
                raise ApiProblem("IDEMPOTENCY_CONFLICT", "resumeId was already used.")
        try:
            name = await human_display_name(me)
        except Exception:
            name = "A teammate"
        now, mode = int(time.time()), _mode(row, own)
        resume = {
            "resumeId": resume_id,
            "byUserId": me,
            "state": "pending",
            "at": now,
            "until": now + RESUME_SECONDS,
            "mode": mode,
        }
        if text:
            resume["text"] = text
        want = {
            **_want_key(row),
            "resumeId": resume_id,
            "sessionId": sid,
            "provider": row["provider"],
            "conversationId": row["nativeConversationId"],
            "byUserId": me,
            "byName": name[:80],
            "title": (row.get("displayTitle") or row.get("title") or row.get("cwdName") or "a session")[:120],
            "mode": mode,
            "at": now,
            "expiresAt": now + RESUME_SECONDS,
        }
        table = store._table().name
        audit = sessions.audit_row(owner, sid, auth, "resume_requested", request_id=resume_id)
        updated = {**row, "resume": resume, "revision": str(uuid.uuid4())}
        try:
            await repo.put_session(
                updated,
                row,
                enrollment,
                extra=[{"Put": {"TableName": table, "Item": want}}, {"Put": {"TableName": table, "Item": audit}}],
            )
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return sessions._session_public(updated, me)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def revoke_resume(auth: AuthContext, session_id: str, user_id: str) -> sessions.LocalSession:
    """The owner takes one person's resume back; a request of theirs still waiting goes with it."""
    from core.services.local_session_relay import publish_resources_changed

    user_id = sessions._valid_user(user_id)
    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await sessions.authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can change who may resume it.")
        allowed = list(row.get("resumeAllowed") or [])
        if user_id not in allowed:
            return sessions._session_public(row, auth.user_id)
        updated = {**row, "resumeAllowed": [u for u in allowed if u != user_id], "revision": str(uuid.uuid4())}
        audit = sessions.audit_row(owner, row["sessionId"], auth, "resume_revoked", subject_user_id=user_id)
        extra = [{"Put": {"TableName": store._table().name, "Item": audit}}]
        if live(row.get("resume")) and row["resume"]["byUserId"] == user_id:
            updated.pop("resume")
            extra.append(_want_delete(row))
        try:
            await repo.put_session(updated, row, enrollment, extra=extra)
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return sessions._session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
