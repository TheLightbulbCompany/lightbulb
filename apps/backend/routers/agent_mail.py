"""Agent mail — container routes (hook-token) + owner settings (Clerk).

Container routes live under ``/u/{owner_id}/…``: the owner is NAMED in the
path and the bearer must timing-safe-match THAT owner's ``containers.hook_token``
(the same model the hooks proxy uses) — a container can only ever act for its
own owner. Policy for WHO a message may go to lives in
``core/services/agent_mail_send.send``; this router only authenticates, rate
limits, validates shapes, and relays the result string.

The per-minute bucket below is burst smoothing ONLY — it is per-process, and
several backend tasks run behind the ALB, so it is not a real quota. The
durable fence is the per-owner daily cap enforced in ``agent_mail_send``.
"""

import asyncio
import hmac
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories import agent_email_repo, container_repo, messaging_repo
from core.repositories.agent_mail_inbox_repo import (
    MAX_TEXT_CHARS,
    count_received_after,
    get_message,
    list_messages,
    list_page,
    reply_key,
)
from core.services import effect_doors, meeting_invites, messaging
from core.services.agent_email import EMAIL_ADDRESS_RE, ensure_team_address, public_for, team_address
from core.services.agent_mail_send import _owner_email, send
from core.services.rate_bucket import RateBucket

router = APIRouter()

RATE_LIMIT_PER_MINUTE = 60
_RATE = RateBucket(RATE_LIMIT_PER_MINUTE)
_INBOX_TEXT_CHARS = 8_000
_DOMAIN_RE = re.compile(r"^@[^@\s]+\.[^@\s]+$")


def _check_rate_limit(owner_id: str) -> bool:
    return _RATE.allow(owner_id)


async def _require_hook_token(owner_id: str, request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    presented = auth[len("Bearer ") :]
    container = await container_repo.get_by_owner_id(owner_id)
    expected = container.get("hook_token") if container else ""
    # Compare bytes, not str: hmac.compare_digest raises TypeError on `str`
    # arguments containing non-ASCII characters, and `presented` comes straight
    # from the raw Authorization header. An unhandled TypeError would surface
    # as a 500 instead of the intended 401. `.encode("utf-8")` on any `str`
    # always succeeds, and comparing bytes is still constant-time.
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail={"status": "invalid_token"})
    if not _check_rate_limit(owner_id):
        raise HTTPException(status_code=429, detail={"status": "rate_limited"})


class SendRequest(BaseModel):
    agent_id: str
    subject: str
    body_text: str
    body_html: str | None = None
    to: str | None = None
    reply_key: str | None = None
    purpose: str = "routine"


@router.post("/u/{owner_id}/send")
async def send_mail(owner_id: str, body: SendRequest, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if body.to and body.reply_key:
        raise HTTPException(status_code=400, detail={"status": "to_and_reply_key"})
    try:  # with the switch on, a send must be one a person allowed (Lane Q door gating); off is today's path
        await effect_doors.require_intent(
            owner_id,
            "mail.send",
            effect_doors.mail_operands(
                agent_id=body.agent_id,
                subject=body.subject,
                body_text=body.body_text,
                to=body.to,
                reply_key=body.reply_key,
            ),
        )
    except effect_doors.DoorRefused as exc:
        raise HTTPException(status_code=403, detail={"status": exc.code, "message": str(exc)}) from exc
    result = await send(
        owner_id=owner_id,
        agent_id=body.agent_id,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        to=body.to,
        reply_key=body.reply_key,
        purpose=body.purpose,
    )
    return {"status": result}


class AddressRequest(BaseModel):
    agent_id: str | None = None  # still sent by the skill's lightbulb-mail.sh; the address is the workspace's


@router.post("/u/{owner_id}/address")
async def mint_address(owner_id: str, body: AddressRequest, request: Request) -> dict:
    """The workspace's team address — the same for every agent, which sends from it as itself."""
    await _require_hook_token(owner_id, request)
    address, reason = await ensure_team_address(owner_id, await _owner_email(owner_id))
    return {"address": await public_for(address) if address else None, "reason": reason}


@router.get("/u/{owner_id}/inbox")
async def inbox(owner_id: str, agent_id: str, request: Request, since: str | None = None, limit: int = 20) -> dict:
    await _require_hook_token(owner_id, request)
    address = await team_address(owner_id)
    if not address:
        return {"address": None, "messages": []}
    rows = await list_messages(address, since=since, limit=limit, agent_id=agent_id)
    return {
        "address": await public_for(address),
        "messages": [
            {
                "key": reply_key(address, r["received_key"]),
                "from": r.get("from_addr", ""),
                "subject": r.get("subject", ""),
                "received_at": r.get("received_at", ""),
                "channel": r.get("channel") or "mail",
                "text": (r.get("text") or "")[:_INBOX_TEXT_CHARS],
            }
            for r in rows
        ],
    }


# ---- owner settings (Clerk) -------------------------------------------------


def _project(row: dict) -> dict:
    return {
        "address": row.get("public_address") or row.get("address"),
        "verified_senders": row.get("verified_senders") or [],
        "send_to_anyone": bool(row.get("send_to_anyone")),
        "footer_enabled": row.get("footer_enabled") is not False,
        "outbound_disabled": row.get("outbound_disabled"),
        "status": row.get("status"),
    }


def _validate_senders(entries: list[str]) -> list[str]:
    out: list[str] = []
    for raw in entries:
        e = (raw or "").strip().lower()
        if not (EMAIL_ADDRESS_RE.match(e) or _DOMAIN_RE.match(e)):
            raise ValueError(e)
        out.append(e)
    return out


async def _team_row(owner_id: str) -> tuple[dict, str | None]:
    """The workspace's one mailbox row, created if absent, and the owner's email
    (None when the lookup fails — that must not block reading or saving)."""
    try:
        owner_email = await _owner_email(owner_id)
    except Exception:  # noqa: BLE001
        owner_email = None
    address, reason = await ensure_team_address(owner_id, owner_email)
    row = await agent_email_repo.get_by_address(address) if address else None
    if not row:
        raise HTTPException(status_code=409, detail={"status": reason})
    return row, owner_email


@router.get("/address")
async def get_address(auth: AuthContext = Depends(get_current_user)) -> dict:
    row, _ = await _team_row(resolve_owner_id(auth))
    return _project(row)


class AddressSettings(BaseModel):
    verified_senders: list[str] | None = None
    send_to_anyone: bool | None = None
    footer_enabled: bool | None = None
    outbound_disabled: str | None = None  # only explicit null is honoured (re-enable)


@router.patch("/address")
async def patch_address(body: AddressSettings, auth: AuthContext = Depends(get_current_user)) -> dict:
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    row, owner_email = await _team_row(owner_id)
    kwargs: dict = {}
    if body.verified_senders is not None:
        try:
            senders = _validate_senders(body.verified_senders)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"status": "bad_sender", "entry": str(e)}) from e
        # The panel tells the owner "You are always allowed", and sender_allowed has no owner
        # special case, so the server keeps that promise: the owner's own address is re-added to
        # whatever list is saved (an empty or edited list can never lock them out of waking their agent).
        owner = agent_email_repo.normalize(owner_email) if owner_email else None
        if owner and owner not in senders:
            senders.append(owner)
        kwargs["verified_senders"] = senders
    if body.send_to_anyone is not None:
        kwargs["send_to_anyone"] = body.send_to_anyone
    if body.footer_enabled is not None:
        kwargs["footer_enabled"] = body.footer_enabled
    if "outbound_disabled" in body.model_fields_set:
        if body.outbound_disabled is not None:
            raise HTTPException(status_code=400, detail={"status": "outbound_disabled_readonly"})
        kwargs["outbound_disabled"] = None
    row = await agent_email_repo.update_settings(row["address"], **kwargs)
    if row is None:
        raise HTTPException(status_code=404, detail={"status": "no_address"})
    return _project(row)


# ---- the team inbox, read by any workspace member (Clerk) -------------------

_PREVIEW_CHARS = 2_000
_MEETINGS_MAX = 50
_TRANSCRIPTS_MAX = 10
_READS_IN_FLIGHT = 8


def _occurrences(meeting: dict) -> list[dict]:
    """Every occurrence with a transcript, newest first: the current one, then ``history``."""
    current = [meeting] if meeting.get("transcriptId") else []
    return [o for o in current + list(meeting.get("history") or []) if o.get("transcriptId")]


def _junk(meeting: dict, now: float) -> bool:
    """A meeting that never reached a call (never in the call, no transcript now or in its history)
    and ended over a day ago is not worth a row."""
    end = int(meeting.get("endAt") or meeting.get("startAt") or 0)
    return (
        meeting.get("status") in ("ended", "bot_failed", "cancelled")
        and not meeting.get("inCallAt")
        and not _occurrences(meeting)
        and end < now - 86400
    )


# Recall sub-codes (stored as ``botError`` by the status webhook) that mean the host kept the bot
# out: knocking off, blocked, or left in / removed from the waiting room.
_NOT_ADMITTED = {
    "google_meet_knocking_disabled",
    "google_meet_bot_blocked",
    "timeout_exceeded_waiting_room",
    "bot_kicked_from_waiting_room",
    "call_ended_by_platform_waiting_room_timeout",
}


def _never_admitted(meeting: dict) -> bool:
    """Stored ``ended`` without ``inCallAt``: the bot left the lobby (timed out, refused, or removed
    before admission). Recall reports that as ``call_ended``; the view shows it as a failed join."""
    return meeting.get("status") == "ended" and not meeting.get("inCallAt")


def _reason(meeting: dict) -> str | None:
    if _never_admitted(meeting):
        return "not_admitted"
    if meeting.get("status") == "bot_failed" and meeting.get("botError") in _NOT_ADMITTED:
        return "not_admitted"
    return None


def _notes_room(room_id: str, meeting: dict, default_room: str | None) -> str | None:
    """Where the current/last occurrence's transcript and notes were posted, only when that is not
    the meeting's own hidden room: the stored room, else (an ended call) the default room."""
    if _never_admitted(meeting):
        return None  # no call happened
    room = meeting.get("transcriptRoomId")
    if not room and meeting.get("status") == "ended" and meeting.get("inCallAt"):
        room = default_room
    if not room and not meeting.get("inCallAt"):  # the current occurrence has none yet: the last one
        room = next(iter(meeting.get("history") or []), {}).get("transcriptRoomId")
    return room if room and room != room_id else None


_UNSAID_FIELDS = {
    "agent_id": "agentId",
    "agent_name": "agentName",
    "request": "request",
    "answer": "answer",
    "at": "at",
}


def _unsaid(occurrence: dict) -> list[dict]:
    return [{k: u.get(c) for k, c in _UNSAID_FIELDS.items()} for u in occurrence.get("unsaid") or []]


def _notes(meeting: dict) -> str | None:
    """The current occurrence's notes: posted (a run was asked for), skipped, pending once a call
    the bot was in is over, else None (no call, or not over yet)."""
    if meeting.get("notesRunId"):
        return "posted"
    if meeting.get("notesSkipped"):
        return "skipped"
    if meeting.get("status") in ("ended", "bot_failed") and meeting.get("inCallAt"):
        return "pending"
    return None


def _occurrence_notes(meeting: dict, occurrence: dict) -> str | None:
    """One occurrence's notes. The current one may still be pending. An archived one is final:
    with no marker (archived before the state was kept, or its notes failed every attempt and the
    series advanced) its state is unknown, never "pending" for ever."""
    if occurrence is meeting:
        return _notes(meeting)
    return "posted" if occurrence.get("notesRunId") else "skipped" if occurrence.get("notesSkipped") else None


def _meeting_view(room_id: str, meeting: dict, default_room: str | None = None) -> dict:
    return {
        "id": room_id,
        "title": meeting.get("title") or "Meeting",
        "organizer": meeting.get("organizer"),
        "invited_by": meeting.get("invitedBy"),
        "status": "bot_failed" if _never_admitted(meeting) else meeting.get("status"),
        "start_at": meeting.get("startAt"),
        "end_at": meeting.get("endAt"),
        "repeats": bool(meeting.get("recurrence")),
        "transcripts": [
            {
                "start_at": o.get("startAt"),
                "ended_at": o.get("endedAt"),
                "notes": _occurrence_notes(meeting, o),
                "unsaid": _unsaid(o),
            }
            for o in _occurrences(meeting)[:_TRANSCRIPTS_MAX]
        ],
        "notes_room_id": _notes_room(room_id, meeting, default_room),
        "reason": _reason(meeting),
        "ended_at": meeting.get("endedAt"),
        "notes": _notes(meeting),
        "unsaid": _unsaid(meeting),
    }


def _mail_view(address: str, r: dict) -> dict:
    out = r.get("direction") == "out"
    return {
        "key": r["received_key"],
        "direction": "out" if out else "in",
        "from": address if out else r.get("from_addr", ""),
        "to": r.get("to", "") if out else address,
        "subject": r.get("subject", ""),
        "at": r.get("received_at", ""),
        "agent_id": r.get("agent_id"),
        "channel": r.get("channel") or "mail",
        "text": (r.get("text") or "")[:_PREVIEW_CHARS],
        "truncated": len(r.get("text") or "") > _PREVIEW_CHARS,
    }


async def _meeting(owner_id: str, meeting_id: str) -> dict | None:
    """The live meeting record, only when it is this workspace's."""
    meta = await messaging_repo.get_room(meeting_id)
    return meta["meeting"] if meta and meta.get("ownerId") == owner_id and meta.get("meeting") else None


@router.get("/inbox")
async def team_inbox(
    before: str | None = None,
    limit: int = Query(30, ge=1, le=50),
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    owner_id = resolve_owner_id(auth)
    # DynamoDB only: no owner-email (Clerk) lookup and no mint; GET /address creates the row.
    address = await team_address(owner_id)
    row = await agent_email_repo.get_by_address(address) if address else None
    if not row:
        raise HTTPException(status_code=409, detail={"status": "no_address"})
    address = row["address"]
    # ponytail: one read per meeting id ever recorded (a string set has no order to take the newest
    # by); upgrade to a batch_get_item or a bounded list when a workspace books hundreds.
    ids = sorted(row.get("meetings") or ())
    gate = asyncio.Semaphore(_READS_IN_FLIGHT)

    async def read(i: str) -> dict | None:
        async with gate:
            return await _meeting(owner_id, i)

    records = await asyncio.gather(*(read(i) for i in ids))
    default_room = None
    if any(m and m.get("status") == "ended" and m.get("inCallAt") for m in records):
        try:  # one read for the whole inbox; output_room answers its 2nd argument when no default stands
            default_room = await meeting_invites.output_room(owner_id, "") or None
        except Exception:  # noqa: BLE001 -- a link is a nicety; the inbox must still load
            default_room = None
    meetings = sorted(
        (
            _meeting_view(i, m, default_room)
            for i, m in zip(ids, records, strict=True)
            if m and not _junk(m, time.time())
        ),
        key=lambda m: int(m["start_at"] or 0),
        reverse=True,
    )[:_MEETINGS_MAX]
    rows, nxt = await list_page(address, before=before, limit=limit)
    shown = row.get("public_address") or address
    return {"address": shown, "meetings": meetings, "messages": [_mail_view(shown, r) for r in rows], "next": nxt}


_NEW_CAP = 10  # the rail badge shows "9+"


@router.get("/inbox/new")
async def team_inbox_new(since: str | None = None, auth: AuthContext = Depends(get_current_user)) -> dict:
    """The rail badge: received mail after ``since`` (an ISO time), capped. One inbox query, no
    meeting reads. A missing or unparsable ``since``, or no team address yet, counts 0."""
    owner_id = resolve_owner_id(auth)
    try:
        mark = datetime.fromisoformat(since or "")
    except ValueError:
        return {"count": 0}
    if mark.tzinfo is None:
        mark = mark.replace(tzinfo=timezone.utc)
    # The browser's mark has millisecond precision; stored keys have microseconds. Past the whole
    # millisecond, so the mail that set the mark does not count as new.
    bound = (mark.astimezone(timezone.utc) + timedelta(microseconds=999)).isoformat()
    address = await team_address(owner_id)
    if not address:
        return {"count": 0}
    return {"count": await count_received_after(address, bound, cap=_NEW_CAP)}


@router.get("/message")
async def team_message(
    key: str = Query(..., min_length=1, max_length=512), auth: AuthContext = Depends(get_current_user)
) -> dict:
    """One team-inbox mail with its full text. Read at the caller's own team address only, so a
    key can never reach another workspace's row."""
    owner_id = resolve_owner_id(auth)
    address = await team_address(owner_id)
    item = await get_message(address, key) if address else None
    if not item or item.get("owner_id") != owner_id:
        raise HTTPException(status_code=404, detail={"status": "not_found"})
    shown = await public_for(address)
    text = item.get("text") or ""
    # Storage keeps the first MAX_TEXT_CHARS; a body at the cap was (almost surely) cut there.
    return {**_mail_view(shown, item), "text": text, "truncated": len(text) >= MAX_TEXT_CHARS}


@router.get("/meetings/{meeting_id}/transcript")
async def meeting_transcript(meeting_id: str, start_at: int, auth: AuthContext = Depends(get_current_user)) -> Response:
    owner_id = resolve_owner_id(auth)
    try:
        meeting_id = str(uuid.UUID(meeting_id))
    except ValueError:
        raise HTTPException(status_code=404, detail={"status": "not_found"}) from None
    meeting = await _meeting(owner_id, meeting_id) or {}
    hit = next((o for o in _occurrences(meeting) if int(o.get("startAt") or 0) == start_at), None)
    if hit is None:
        raise HTTPException(status_code=404, detail={"status": "not_found"})
    room_id = hit.get("transcriptRoomId") or await meeting_invites.output_room(owner_id, meeting_id)
    try:
        _, data = await run_in_thread(messaging.load_attachment, owner_id, room_id, hit["transcriptId"])
    except messaging.ApiProblem:
        raise HTTPException(status_code=404, detail={"status": "not_found"}) from None
    return Response(content=data, media_type="text/plain; charset=utf-8")
