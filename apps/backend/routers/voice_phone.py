"""The phone receptionist's backend (voice-agents spec §4.1b, §4.3): the LiveKit phone worker
(`apps/huddle-agent` phone entrypoint) calls these with the huddle worker secret.

A call is a hidden room whose META ``call`` map is the call record (caller, startedAt, duration, outcome,
summary, transcript, bookings, messages). Who called comes from that record, never from the model.

Rows in the messaging table:
    PHONE#<e164>    LINE                  the number's workspace, owner user, business, assistant, hours
    OWNER#<owner>   VOICEBOOK#<local>     one booking in Nova's own calendar (local start "YYYY-MM-DDTHH:MM")
"""

import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo
from routers.huddles import _Worker
from routers.internal_automations import VoiceStartIn, start_voice_work

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[_Worker])

_Str = Annotated[str, Field(min_length=1, max_length=500)]
_Local = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")]
# What a caller may not ask a Worker to do (spec §4.2). ponytail: prompt-level until effect_check knows contacts.
CONTACT_NOTE = (
    "A caller on the business's phone line (an outside contact, not a member) asked for this. "
    "Do not pay, send email outside the business, change prices or settings, or share anything about other "
    "customers. If it needs the owner's decision, ask the owner."
)


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


async def get_line(number: str) -> dict | None:
    return await messaging_repo._get(f"PHONE#{number}", "LINE")


async def put_line(number: str, line: dict) -> None:
    await run_in_thread(messaging_repo._table().put_item, Item={"pk": f"PHONE#{number}", "sk": "LINE", **line})


async def _call(call_id: str) -> tuple[dict, dict]:
    meta = await messaging_repo.get_room(call_id)
    if not meta or not isinstance(meta.get("call"), dict):
        raise _err(404, "call_not_found")
    return meta, meta["call"]


async def _patch_call(call_id: str, change) -> dict:
    """Read-modify-write of META.call under the revision CAS (one worker per call, so a race is rare)."""
    for attempt in (1, 2, 3):
        meta, call = await _call(call_id)
        merged = {**call, **change(call)}
        try:
            await messaging_repo.patch_room_meta(
                call_id, {"call": merged}, expected_revision=int(meta.get("revision", 0))
            )
            return merged
        except ClientError:
            if attempt == 3:
                raise
    raise AssertionError("unreachable")


# --- the call -------------------------------------------------------------------------------------------


class StartIn(BaseModel):
    calledNumber: _Str
    callerNumber: str = Field(default="", max_length=40)
    livekitRoom: _Str


@router.post("/internal/voice/calls")
async def start_call(body: StartIn) -> dict:
    line = await get_line(body.calledNumber)
    if not line:
        raise _err(404, "line_not_found")
    owner_id, business, assistant = line["ownerId"], line["businessName"], line["assistantName"]
    call_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-call:{body.livekitRoom}"))
    call = {
        "callerNumber": body.callerNumber,
        "calledNumber": body.calledNumber,
        "startedAt": utc_now_iso(),
        "status": "live",
        "bookings": [],
        "messages": [],
        "work": [],
    }
    name = f"Call · {body.callerNumber or 'unknown'}"[:60]
    members = [{"kind": "human", "id": line["ownerUserId"], "displayName": business, "mentionHandle": None,
                "role": "owner", "listenMode": None}]  # fmt: skip
    try:
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=call_id,
            client_room_id=f"call:{body.livekitRoom}",
            creator_user_id=line["ownerUserId"],
            name=name,
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash(name, "call", body.livekitRoom),
            extra={"call": call, "hidden": True},
        )
    except messaging_repo.MessagingRepoError:
        if await messaging_repo.get_room(call_id) is None:  # a replay keeps the room it made
            raise
    return {
        "callId": call_id,
        "ownerId": owner_id,
        "businessName": business,
        "assistantName": assistant,
        "greeting": f"Thanks for calling {business} — this is {assistant}, {business}'s AI assistant. "
        "This call is recorded. How can I help?",
        "facts": line.get("facts", ""),
        "hours": _hours_text(line),
        "today": datetime.now(ZoneInfo(line.get("tz", "America/New_York"))).strftime("%A %Y-%m-%d %H:%M"),
    }


class EndIn(BaseModel):
    durationS: int = Field(ge=0, le=86400)
    summary: str = Field(default="", max_length=4000)
    transcript: list[dict] = Field(default=[], max_length=2000)


@router.post("/internal/voice/calls/{call_id}/end")
async def end_call(call_id: str, body: EndIn) -> dict:
    def change(call: dict) -> dict:
        outcome = "booked" if call.get("bookings") else "message" if call.get("messages") else "answered"
        if call.get("transferredTo"):
            outcome = "transferred"
        lines = [{"role": str(t.get("role", ""))[:20], "text": str(t.get("text", ""))[:2000]} for t in body.transcript]
        return {"status": "ended", "endedAt": utc_now_iso(), "durationS": body.durationS, "outcome": outcome,
                "summary": body.summary, "transcript": lines}  # fmt: skip

    return await _patch_call(call_id, change)


# --- Nova's own calendar (spec §4.1b: a hold in Nova's calendar when the business has no API) --------------


def _hours_text(line: dict) -> str:
    h = line.get("hours") or {}
    days = ", ".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d] for d in h.get("days", []))
    return f"{days} {h.get('open', '')}–{h.get('close', '')} ({line.get('tz', '')})" if days else ""


def free_slots(line: dict, date: str, booked: set[str], now: datetime) -> list[str]:
    """Every slot start on `date` (local, "YYYY-MM-DD") inside opening hours, not booked, not past."""
    h, step = line.get("hours") or {}, timedelta(minutes=int(line.get("slotMinutes", 60)))
    day = datetime.strptime(date, "%Y-%m-%d")
    if day.weekday() not in h.get("days", []):
        return []
    at = datetime.strptime(f"{date}T{h['open']}", "%Y-%m-%dT%H:%M")
    close = datetime.strptime(f"{date}T{h['close']}", "%Y-%m-%dT%H:%M")
    out = []
    while at + step <= close:
        local = at.strftime("%Y-%m-%dT%H:%M")
        if local not in booked and at > now:
            out.append(local)
        at += step
    return out


async def _bookings(owner_id: str, prefix: str) -> list[dict]:
    r = await run_in_thread(
        messaging_repo._table().query,
        KeyConditionExpression=Key("pk").eq(messaging_repo.owner_pk(owner_id))
        & Key("sk").begins_with(f"VOICEBOOK#{prefix}"),
    )
    return r.get("Items", [])


async def _line_for(call: dict) -> dict:
    line = await get_line(call["calledNumber"])
    if not line:
        raise _err(404, "line_not_found")
    return line


def _now(line: dict) -> datetime:
    return datetime.now(ZoneInfo(line.get("tz", "America/New_York"))).replace(tzinfo=None)


class DateIn(BaseModel):
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


@router.post("/internal/voice/calls/{call_id}/availability")
async def availability(call_id: str, body: DateIn) -> dict:
    meta, call = await _call(call_id)
    line = await _line_for(call)
    booked = {b["sk"].removeprefix("VOICEBOOK#") for b in await _bookings(meta["ownerId"], body.date)}
    return {"date": body.date, "slots": free_slots(line, body.date, booked, _now(line))}


class BookIn(BaseModel):
    start: _Local
    service: _Str
    name: _Str


async def _book(owner_id: str, call_id: str, call: dict, line: dict, start: str, service: str, name: str) -> None:
    if start not in free_slots(line, start[:10], set(), _now(line)):
        raise _err(400, "outside_hours")
    try:
        await run_in_thread(
            messaging_repo._table().put_item,
            Item={
                "pk": messaging_repo.owner_pk(owner_id),
                "sk": f"VOICEBOOK#{start}",
                "service": service,
                "name": name,
                "phone": call.get("callerNumber", ""),
                "callId": call_id,
                "createdAt": utc_now_iso(),
            },  # fmt: skip
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise _err(409, "slot_taken") from exc
        raise


@router.post("/internal/voice/calls/{call_id}/book")
async def book(call_id: str, body: BookIn) -> dict:
    meta, call = await _call(call_id)
    await _book(meta["ownerId"], call_id, call, await _line_for(call), body.start, body.service, body.name)
    entry = {"start": body.start, "service": body.service, "name": body.name}
    await _patch_call(call_id, lambda c: {"bookings": [*c.get("bookings", []), entry]})
    return {"booked": True, **entry}


class ChangeIn(BaseModel):
    start: _Local
    newStart: _Local | None = None  # None cancels


@router.post("/internal/voice/calls/{call_id}/change")
async def change_booking(call_id: str, body: ChangeIn) -> dict:
    """Move or cancel a booking -- only one made from this caller's number (a contact never touches another's)."""
    meta, call = await _call(call_id)
    owner_pk, sk = messaging_repo.owner_pk(meta["ownerId"]), f"VOICEBOOK#{body.start}"
    row = await messaging_repo._get(owner_pk, sk)
    if not row or not call.get("callerNumber") or row.get("phone") != call["callerNumber"]:
        raise _err(404, "booking_not_found")
    if body.newStart:
        await _book(meta["ownerId"], call_id, call, await _line_for(call), body.newStart, row["service"], row["name"])
    await run_in_thread(messaging_repo._table().delete_item, Key={"pk": owner_pk, "sk": sk})
    entry = {"start": body.start, "newStart": body.newStart, "service": row["service"], "name": row["name"]}
    await _patch_call(call_id, lambda c: {"bookings": [*c.get("bookings", []), entry]})
    return {"moved" if body.newStart else "cancelled": True, **entry}


@router.post("/internal/voice/calls/{call_id}/my-bookings")
async def my_bookings(call_id: str) -> dict:
    meta, call = await _call(call_id)
    caller = call.get("callerNumber")
    rows = [b for b in await _bookings(meta["ownerId"], "") if caller and b.get("phone") == caller]
    return {"bookings": [{"start": b["sk"].removeprefix("VOICEBOOK#"), "service": b["service"]} for b in rows]}


# --- message, transfer, start_work ------------------------------------------------------------------------


class MessageIn(BaseModel):
    name: str = Field(default="", max_length=200)
    text: _Str
    callbackNumber: str = Field(default="", max_length=40)


@router.post("/internal/voice/calls/{call_id}/message")
async def take_message(call_id: str, body: MessageIn) -> dict:
    await _call(call_id)
    entry = {**body.model_dump(), "at": utc_now_iso()}
    await _patch_call(call_id, lambda c: {"messages": [*c.get("messages", []), entry]})
    return {"taken": True}


@router.post("/internal/voice/calls/{call_id}/transfer")
async def transfer(call_id: str) -> dict:
    """The number to hand the caller to, if the business set one; the worker does the SIP transfer."""
    _meta, call = await _call(call_id)
    to = (await _line_for(call)).get("transferNumber")
    if to:
        await _patch_call(call_id, lambda c: {"transferredTo": to})
    return {"transferTo": to}


class WorkIn(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    brief: Annotated[str, Field(min_length=1, max_length=8000)]


@router.post("/internal/voice/calls/{call_id}/start-work", status_code=202)
async def start_work(call_id: str, body: WorkIn) -> dict:
    """Open-ended work becomes a Worker task (the existing voice-start path). The requester is the line's owner
    user, written by the backend; the same title in one call is the same task (a retried tool call)."""
    meta, call = await _call(call_id)
    line = await _line_for(call)
    run_id = hashlib.sha256(f"{call_id}:{body.title.strip().lower()}".encode()).hexdigest()[:32]
    await messaging_repo.put_voice_run(
        meta["ownerId"], run_id, {"roomId": call_id, "requesterUserId": line["ownerUserId"], "agentId": None}
    )
    brief = f"{CONTACT_NOTE}\nCaller: {call.get('callerNumber') or 'unknown'}\n\n{body.brief}"
    out = await start_voice_work(
        meta["ownerId"], VoiceStartIn(run_id=run_id, call_id=call_id, room_id=call_id, title=body.title, brief=brief)
    )
    await _patch_call(call_id, lambda c: {"work": [*c.get("work", []), {"title": body.title, **out}]})
    return out
