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

import json
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


#: Acknowledgement entries, newest kept: the relay envelope is 16 KiB.
ACK_CAP = 8
WANT_FIELDS = ("resumeId", "sessionId", "provider", "conversationId", "byUserId", "byName", "title", "mode")


async def ack_with_resume(ack: dict, owner: str, human: str, device: str) -> dict:
    """Add this device's waiting resumes to a heartbeat acknowledgement, beside pick-up's
    (``session_pickup.ack_with_pickup``). Installed desktops ignore the extra key; an empty list
    is omitted."""
    try:
        rows = await store._query(repo.user_pk(owner, human), begins_with=f"RESUMEWANT#{device}#")
    except Exception:
        logger.warning("Session resume wants unavailable")  # never costs the heartbeat
        return ack
    now = int(time.time())
    entries = [
        {key: r[key] for key in WANT_FIELDS}
        for r in sorted(rows, key=lambda r: r.get("at", 0))
        if int(r.get("expiresAt", 0)) > now  # TTL deletes lazily
    ][-ACK_CAP:]
    while entries:
        candidate = {**ack, "resume": entries}
        if len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) <= sessions.MAX_ENVELOPE_BYTES:
            return candidate
        entries = entries[1:]  # oldest first
    return ack


class ResumeFailed(ContractModel):
    resume_id: str
    code: Annotated[str, Field(pattern=r"^[A-Z_]{1,32}$")]


async def authorize_device(token: str, session_id: str) -> dict | None:
    """The paired Mac that owns this session, for its own human. None on any failure."""
    from core.services.machine_auth import _device

    identity = await _device(token, "openclaw-macos")
    if identity is None or identity.device_id is None:
        return None
    try:
        sid = validate_uuid(session_id, label="local session ID")
    except ApiProblem:
        return None
    row = await repo.get_session(identity.owner_id, sid)
    if row is None or row["humanUserId"] != identity.user_id or row["deviceId"] != identity.device_id:
        return None
    return row


async def mark_failed(row: dict, failed: ResumeFailed) -> None:
    """The Mac would not start it: say why, and stop asking. A request that has moved on (a newer
    one, taken back, expired) is left alone."""
    from core.services.local_session_relay import publish_resources_changed

    for _ in range(6):
        current = await repo.get_session(row["ownerId"], row["sessionId"])
        waiting = (current or {}).get("resume")
        if not live(waiting) or waiting["resumeId"] != failed.resume_id:
            return
        refused = {k: v for k, v in waiting.items() if k != "text"} | {"state": "failed", "code": failed.code}
        update = {
            "TableName": store._table().name,
            "Key": {"pk": current["pk"], "sk": "META"},
            "UpdateExpression": "SET #resume = :resume, revision = :new",
            "ConditionExpression": "revision = :old",
            "ExpressionAttributeNames": {"#resume": "resume"},
            "ExpressionAttributeValues": {":resume": refused, ":new": str(uuid.uuid4()), ":old": current["revision"]},
        }
        try:
            await store._transact([{"Update": update}, _want_delete(current)])
            break
        except ClientError as exc:
            if not store._is_cancelled(exc):
                raise
    else:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
    await publish_resources_changed(row["ownerId"], row["humanUserId"])


async def on_register(previous: dict | None, row: dict) -> list[dict]:
    """Every registration, before its write. A registration un-ends the session, so a waiting
    request leaves the row either way. The first registration of a NEW process while one waits is
    that resume: the resumer joins ``allowedSenders`` and their first message is admitted as an
    ordinary send, both in the registration's own transaction -- it exists once or not at all,
    and the relay's dispatch delivers it at most once. Mutates ``row``; returns the extra
    transaction items. Reads only the generation, so it holds with or without a ``holderId``."""
    waiting = row.pop("resume", None)
    if previous is None or not live(waiting):
        return []
    items = [_want_delete(previous)]
    if previous["generation"] == row["generation"]:
        return items  # the same process came back: nothing was resumed
    owner, sid, by = row["ownerId"], row["sessionId"], waiting["byUserId"]
    if by != row["humanUserId"]:
        stream = await store.get_user_stream(owner, by)
        if not stream or not stream.get("active"):
            return items  # they left the workspace while it waited
        row["allowedSenders"] = [*(row.get("allowedSenders") or []), by]
    if waiting.get("text"):
        now = int(time.time())
        request = sessions.LocalSessionCommand(
            requestId=waiting["resumeId"], expectedGeneration=row["generation"], kind="send", text=waiting["text"]
        )
        payload = request.model_dump(by_alias=True)
        row["commandSeq"] = row.get("commandSeq", 0) + 1
        command = {
            **payload,
            "pk": row["pk"],
            "sk": f"COMMAND#{request.request_id}",
            "sessionId": sid,
            "roomId": "",
            "actorUserId": by,
            "commandSeq": row["commandSeq"],
            "state": "pending",
            "createdAt": now,
            "deliverBy": now + 30,
            # submit_command's own hash: the same request sent again is recognised, never doubled.
            "requestHash": store.request_hash(owner, sid, "", by, payload),
        }
        auth = AuthContext(user_id=by, org_id=owner if owner != by else None)
        audit = sessions.audit_row(owner, sid, auth, "send", request_id=request.request_id)
        table = store._table().name
        items += [
            {"Put": {"TableName": table, "Item": command, "ConditionExpression": "attribute_not_exists(sk)"}},
            {"Put": {"TableName": table, "Item": audit}},
        ]
    return items
