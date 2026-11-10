"""The phone receptionist's backend (voice-agents spec §4.1b, §4.3): the LiveKit phone worker
(`apps/huddle-agent` phone entrypoint) calls these with the huddle worker secret.

A call is a hidden room with META `kind == "call"` whose `call` map is the record the owner's Home reads
(routers/voice.py): caller, callerName, returning, startedAt, durationSec, outcome, summary, booking, steps,
transcript, callerMemory, ownerMode, resolvedAt (plus calledNumber, bookings, messages, work, unknown). Who called
comes from that record, never from the model. The assistant's settings are the Agent page document
(`messaging_repo.get_voice_agent`); its `allow.*` and `paused` gate the tools here, server-side.

Rows in the messaging table:
    PHONE#<e164>    LINE                  the number's workspace (ownerId) and the member tasks run as (ownerUserId)
    OWNER#<owner>   VOICEBOOK#<local>     one booking in the assistant's own calendar (local start "YYYY-MM-DDTHH:MM");
                                          with bookingsGo "google" it is the hold, eventId its Google event
                                          (core/services/voice_booking)
    OWNER#<owner>   VOICECALLER#<e164>    a caller seen before: name, last call, facts (<= 10, newest first), visits
    OWNER#<owner>   VOICELIVE#<line>#<i>  a live-call slot (LIVE_CALLS_PER_LINE), held by a call id until its end
    OWNER#<owner>   VOICECALLS#<day>#<c>  calls from caller c today; VOICEBOOKS#<day>#<c> its bookings ("withheld": "")
    OWNER#<owner>   VOICETURNS#<call>     the call's `/turn` numbers (TURNS_PER_CALL)
"""

import asyncio
import functools
import hashlib
import hmac
import re
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext
from core.dynamodb import run_in_thread, utc_now_iso
from core.billing import voice_billing, voice_plan
from core.observability.metrics import put_metric
from core.repositories import messaging_repo, tasks_repo, billing_repo
from core import tasks_schema as ts
from core.services import (
    push_service,
    admission,
    clerk_admin,
    messaging,
    phone_pin,
    questions,
    task_conversation,
    voice_booking,
    voice_calls,
    voice_outbound,
    voice_preferences,
)
from routers.huddles import _Worker
from routers.voice import _E164, _e164, month_seconds, us_number
from routers.voice_meet import QuestionRef, agent_lines, answer_said_question, latest_seq
from routers.internal_automations import VoiceStartIn, start_voice_work

logger = logging.getLogger(__name__)

# --- abuse caps (security review 9 Nov, M5). Over one, the call is still answered, but only takes a message. ---------
LIVE_CALLS_PER_LINE = 5  # calls on one line at once
CALLS_PER_CALLER_PER_DAY = 20  # calls from one number to one workspace per UTC day (withheld callers share one)
BOOKINGS_PER_CALLER_PER_DAY = 5  # bookings from one number per UTC day (withheld callers share one)
TURNS_PER_CALL = 60  # the caller's lines delegated to the Router (`/turn`) in one call
LIVE_SLOT_S = 25 * 60  # a live-call slot frees itself after this (the worker's 20 min cap + grace) if no end came
END_GRACE_S = 30  # billed durationSec may exceed the server-measured time since startedAt by this much
#: What carriers present for a withheld number (Twilio: "anonymous", "restricted", "unknown", "private",
#: "unavailable", and the keypad spellings ANONYMOUS, BLOCKED, RESTRICTED, UNAVAILABLE). Stored as caller "".
WITHHELD = frozenset({"anonymous", "restricted", "unknown", "private", "unavailable", "blocked",
                      "+266696687", "+2562533", "+7378742833", "+86282452253"})  # fmt: skip
CALL_TOKEN_HEADER = "X-Lightbulb-Call-Token"


async def _require_call_token(request: Request, x_lightbulb_call_token: Annotated[str, Header()] = "") -> None:
    """Every per-call route needs the token `start_call` returned for that call, not just the worker secret (M4)."""
    call_id = request.path_params.get("call_id")
    if call_id is None:
        return
    expected = str((await messaging_repo.get_room(call_id) or {}).get("callToken") or "")
    if not expected or not hmac.compare_digest(x_lightbulb_call_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail={"code": "invalid_call_token"})


router = APIRouter(dependencies=[_Worker, Depends(_require_call_token)])


def caller_id(raw: str) -> str:
    """The caller as the record keeps it: E.164, or "" for a withheld or unreadable number (M1). "" has no caller
    memory and no bookings to list, move or cancel; it can still book and leave a message."""
    n = raw.strip()
    return n if _E164.fullmatch(n) and n not in WITHHELD else ""


def _day() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")


async def _limit(call_id: str, why: str) -> None:
    """A cap was hit mid-call: from now on the call only takes messages."""
    await _patch_call(
        call_id,
        lambda c: {} if c.get("limited") else {"limited": why, "steps": _step(c, f"Limited to a message: {why}")},
    )


_Str = Annotated[str, Field(min_length=1, max_length=500)]
_Local = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")]
#: Said to the Worker too; effect_check enforces it (a phone_contact task asks the owner for every outside action).
CONTACT_NOTE = (
    "A caller on the business's phone line (an outside contact, not a member) asked for this. "
    "Do not pay, send email outside the business, change prices or settings, or share anything about other "
    "customers. If it needs the owner's decision, ask the owner."
)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: The Agent page's defaults (apps/frontend/src/components/voice/data.ts DEFAULT_CONFIG), for what this file reads.
DEFAULTS: dict = {
    "name": "",
    "greeting": "",
    "voice": voice_preferences.DEFAULT_VOICE,
    "spanish": False,
    "live": False,
    "paused": False,
    "instructions": "",
    "allow": {"book": True, "move": True, "greetByName": True, "answer": True},
    "knowledge": [],
    "hours": {d: {"open": "09:00", "close": "18:00", "closed": d == "Sun"} for d in _DAYS},
    "holidays": [],  # closures: [{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}], local dates; an old free-text value means none
    "services": [],
    "bufferMin": 10,
    "timezone": "America/New_York",  # IANA; Onboarding saves the browser's
    "handoff": {"number": "", "onlyOpenHours": True},
    "forwardFrom": "",
}
_TZ = ZoneInfo("America/New_York")  # the call clock (startedAt, step times); bookings and hours use _tz(cfg)
_GRID = timedelta(minutes=30)  # slot starts every half hour
_warming: set[asyncio.Task] = set()  # held so the Google warm-up is not garbage-collected mid-flight


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


async def get_line(number: str) -> dict | None:
    return await messaging_repo._get(f"PHONE#{number}", "LINE")


async def put_line(number: str, line: dict) -> None:
    await run_in_thread(messaging_repo._table().put_item, Item={"pk": f"PHONE#{number}", "sk": "LINE", **line})


async def agent_config(owner_id: str) -> dict:
    cfg = {**DEFAULTS, **(await messaging_repo.get_voice_agent(owner_id) or {})}
    for k in ("allow", "hours", "handoff"):
        cfg[k] = {**DEFAULTS[k], **(cfg.get(k) or {})}
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    cfg["_paymentBlocked"] = bool(
        account.get("metronome_voice_contract_id") and not voice_billing.has_voice_plan(account)
    )
    return cfg


def _line_state(cfg: dict, caller: str) -> Literal["live", "message_only"]:
    """Live after Go live and not paused; the owner's own number always gets the live line (the test call)."""
    if cfg.get("_paymentBlocked"):
        return "message_only"
    owner = {_e164(str(n or "")) for n in (cfg["handoff"]["number"], cfg.get("forwardFrom"))} - {None}
    return "live" if (cfg.get("live") and not cfg.get("paused")) or _e164(caller) in owner else "message_only"


def call_mode(cfg: dict, call: dict) -> Literal["live", "message_only"]:
    """The line's state for this call; a call over an abuse cap (`limited`) only takes messages."""
    return "message_only" if call.get("limited") else _line_state(cfg, call.get("caller", ""))


def _allowed(cfg: dict, call: dict, *caps: str) -> None:
    """Refuse (403) a capability the owner turned off, or everything but a message while the line is message-only."""
    if call_mode(cfg, call) != "live" or not any(cfg["allow"].get(c) for c in caps):
        raise _err(403, "not_allowed")


async def _call(call_id: str) -> tuple[dict, dict]:
    meta = await messaging_repo.get_room(call_id)
    if not meta or meta.get("kind") != "call" or not isinstance(meta.get("call"), dict):
        raise _err(404, "call_not_found")
    return meta, meta["call"]


# A card number: 13-19 digits, spaces or dashes between them, Luhn-valid; then an expiry or CVV said after it.
_CARD_RUN = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,}(?!\d)")
_CARD_TAIL = re.compile(
    r"(?:\W|and|exp(?:iry|ires|iration)?|cvv2?|cvc|security|code|date|is|the|on|back)*"
    r"(?:\b(?:0?[1-9]|1[0-2])\s*/\s*(?:\d{4}|\d{2})\b|\b\d{3,4}\b)",
    re.IGNORECASE,
)
CARD_REDACTED = "[card number removed]"


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(int(c) for c in reversed(digits)):
        total += d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2)
    return total % 10 == 0


def redact_cards(text: str) -> str:
    """No card capture (8 Nov prod: a message step kept the caller's digits): every Luhn-valid 13-19 digit run is
    replaced, and up to two expiry or CVV groups said right after it ("exp 12/27, cvv 123"). The call record and
    the call room's messages are written through this (`_scrub`)."""
    out, at = [], 0
    for m in _CARD_RUN.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        # the longest Luhn-valid 13-19 digit prefix: a CVV said straight after the number joins its run
        n = next((n for n in range(min(len(digits), 19), 12, -1) if _luhn(digits[:n])), 0)
        if not n:
            continue
        end = m.start() + [i for i, c in enumerate(m.group()) if c.isdigit()][n - 1] + 1  # after its n-th digit
        tail_start = end
        for _ in range(2):
            t = _CARD_TAIL.match(text, end)
            if not t:
                break
            end = t.end()
        out += [text[at : m.start()], CARD_REDACTED, " [removed]" if end > tail_start else ""]
        at = end
    return "".join(out) + text[at:] if out else text


def _scrub(value):
    """`redact_cards` on every string in a record (lists and maps too)."""
    if isinstance(value, str):
        return redact_cards(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


#: What a model writes when it has no name (8 Nov prod: "Unknown" was kept as the caller's name).
_NO_NAMES = {"unknown", "unknown caller", "caller", "the caller", "anonymous", "anon", "n/a", "na", "none", "null",
             "no name", "not given", "not provided", "unknown name", "name unknown", "customer", "-", "?"}  # fmt: skip


def first_name(call: dict) -> str:
    """The only caller detail a push may carry."""
    return (real_name(call.get("callerName")).split() or [""])[0]


def real_name(name: str | None) -> str:
    """The caller's name, or "" for a placeholder: never stored as who called."""
    n = " ".join(str(name or "").split())
    return "" if n.lower().strip(".") in _NO_NAMES else n


async def _patch_call(call_id: str, change) -> dict:
    """Read-modify-write of META.call under the revision CAS (one worker per call, so a race is rare). Every write
    of the record (steps, transcript, messages, summary) is card-scrubbed and keeps only a real caller name."""
    for attempt in (1, 2, 3):
        meta, call = await _call(call_id)
        merged = _scrub({**call, **change(call)})
        merged["callerName"] = real_name(merged.get("callerName"))
        try:
            await messaging_repo.patch_room_meta(
                call_id, {"call": merged}, expected_revision=int(meta.get("revision", 0))
            )
            return merged
        except ClientError:
            if attempt == 3:
                raise
    raise AssertionError("unreachable")


def _step(call: dict, text: str) -> list[dict]:
    """The call's steps plus one, `t` seconds into the call (the Home shows it as m:ss)."""
    try:
        t = int((datetime.now(_TZ) - datetime.fromisoformat(call["startedAt"])).total_seconds())
    except (KeyError, ValueError):
        t = 0
    return [*call.get("steps", []), {"t": max(t, 0), "text": text, "guessed": False}]


def _pretty(local: str) -> str:
    return datetime.strptime(local, "%Y-%m-%dT%H:%M").strftime("%a %-d %b, %-I:%M %p")


def _caller_key(owner_id: str, caller: str) -> dict:
    return {"pk": messaging_repo.owner_pk(owner_id), "sk": f"VOICECALLER#{caller}"}


async def _seen(owner_id: str, caller: str) -> dict | None:
    return await messaging_repo._get(**_caller_key(owner_id, caller)) if caller else None


# --- the call -------------------------------------------------------------------------------------------


class StartIn(BaseModel):
    calledNumber: _Str
    callerNumber: str = Field(default="", max_length=40)
    livekitRoom: _Str


def _facts(cfg: dict) -> str:
    """What the assistant may tell callers: services, prices, hours and the owner's answers (allow.answer)."""
    services = "; ".join(
        f"{s.get('name')}" + (f" {s['price']}" if s.get("price") else "") + (f" ({s['minutes']} min)" if s.get("minutes") else "")
        for s in cfg["services"] if s.get("name")
    )  # fmt: skip
    hours = ", ".join(
        f"{d} {h['open']}–{h['close']}" if not h.get("closed") else f"{d} closed" for d, h in cfg["hours"].items()
    )
    qa = " ".join(
        f"Q: {k['q']} A: {k['a']}" if k.get("q") else f"Note: {k['a']}" for k in cfg["knowledge"] if k.get("a")
    )
    return f"Services: {services or 'not listed'}. Opening hours: {hours}. {qa}".strip()


@router.post("/internal/voice/calls")
async def start_call(body: StartIn) -> dict:
    line = await get_line(body.calledNumber)
    if not line:
        raise _err(404, "line_not_found")
    owner_id = line["ownerId"]
    cfg = await agent_config(owner_id)
    if _is_probe(cfg, body):
        # Our own forwarding check came back: no caller is on it, so no greeting is owed.
        raw = await messaging_repo.get_voice_agent(owner_id) or {}
        raw.pop("forwardingCheck", None)
        await messaging_repo.put_voice_agent(
            owner_id, {**raw, "forwardingOk": True, "forwardingOkAt": utc_now_iso()}, "voice"
        )
        await voice_outbound.end_probe(body.livekitRoom, cfg["forwardingCheck"]["callId"])
        raise _err(409, "forward_check")  # the worker leaves without speaking
    org = await clerk_admin.get_organization(owner_id) if owner_id.startswith("org_") else None
    business, assistant = (org or {}).get("name") or "the business", cfg["name"].strip() or "Lightbulb"
    if cfg.get("bookingsGo") == "google":  # a cold grant takes seconds; the in-call Google calls get 2 s
        _warming.add(t := asyncio.create_task(voice_booking.warm_google(owner_id)))
        t.add_done_callback(_warming.discard)
    caller = caller_id(body.callerNumber)
    seen = await _seen(owner_id, caller)
    call_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-call:{body.livekitRoom}"))
    existing = await messaging_repo.get_room(call_id)  # a replayed start: its token and its caps, counted once
    token = (existing or {}).get("callToken") or secrets.token_urlsafe(32)
    slot = (existing or {}).get("liveSlot") or (
        None if existing else await _live_slot(owner_id, body.calledNumber, call_id)
    )
    if existing:
        limited = (existing.get("call") or {}).get("limited") or ""
    elif not slot:
        limited = "too many calls on the line at once"
    elif not await messaging_repo.take_voice_unit(  # keyed by call id: overlapping starts of one call count once
        owner_id, f"VOICECALLS#{_day()}#{caller or 'withheld'}", call_id, CALLS_PER_CALLER_PER_DAY
    ):
        limited = "too many calls from this number today"
    else:
        limited = ""
    # Minutes used up and the owner chose "forward": the caller goes to the hand-off number instead of the assistant.
    # Never to the line that forwards here (the kept business number) or the one called: that rings us again.
    forward_to = (
        us_number(str(cfg["handoff"]["number"] or ""))
        if (cfg.get("runOut") == "forward" or cfg.get("_paymentBlocked")) and not limited
        else None
    )
    if forward_to in {_e164(str(cfg.get("forwardFrom") or "")), _e164(body.calledNumber)} or (
        cfg.get("_paymentBlocked") and forward_to == _e164(body.callerNumber)
    ):
        forward_to = None
    if forward_to and not cfg.get("_paymentBlocked"):
        try:
            account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
            seconds = (
                (await voice_plan.usage(account))["usedSeconds"]
                if account.get("metronome_voice_contract_id")
                else await month_seconds(owner_id, cfg.get("timezone"))
            )
            if seconds < voice_billing.INCLUDED_MINUTES * 60:
                forward_to = None
        except Exception:  # metering never drops a live call: answer as normal
            logger.warning("run-out check failed for %s; answering", owner_id, exc_info=True)
            forward_to = None
    call = {
        "caller": caller,
        "callerName": real_name((seen or {}).get("name")),
        "returning": bool(seen),
        "startedAt": datetime.now(_TZ).isoformat(),
        "durationSec": 0,
        "outcome": "answered",
        "summary": "",
        "booking": "",
        "steps": [],
        "transcript": [],
        "callerMemory": list((seen or {}).get("facts", [])) if cfg["allow"]["greetByName"] else [],
        "ownerMode": False,
        "resolvedAt": None,
        "calledNumber": body.calledNumber,
        "status": "live",
        "bookings": [],
        "messages": [],
        "work": [],
        "unknown": [],
        **({"limited": limited} if limited else {}),
    }
    name = f"Call · {call['callerName'] or caller or 'unknown'}"[:60]
    members = [{"kind": "human", "id": line["ownerUserId"], "displayName": business, "mentionHandle": None,
                "role": "owner", "listenMode": None}]  # fmt: skip
    front = None
    if not forward_to:
        try:  # Lightbulb (the Router's voice) is seated, so its replies and the Front desk's answers land here
            front = await task_conversation._front_agent(owner_id)
            members.append(front)
        except Exception:  # noqa: BLE001 -- no front agent yet: the call is still answered; the caller's lines wait
            logger.warning("call room without Lightbulb ws=%s", owner_id, exc_info=True)
    try:
        room = await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=call_id,
            client_room_id=f"call:{body.livekitRoom}",
            creator_user_id=line["ownerUserId"],
            name=name,
            kind="call",
            members=members,
            req_hash=messaging_repo.request_hash(name, "call", body.livekitRoom),
            extra={
                "call": call,
                "hidden": True,
                "ownerUserId": line["ownerUserId"],
                "callToken": token,  # beside the record, never in it: the Home reads META.call
                **({"liveSlot": slot} if slot else {}),
                **({"frontAgentId": front["id"]} if front else {}),
            },
        )
    except messaging_repo.MessagingRepoError:
        if (room := await messaging_repo.get_room(call_id)) is None:  # a replay keeps the room it made
            raise
    # Two overlapping starts for one LiveKit room each minted a token; only the stored one opens the call's routes,
    # so the answer is always the persisted META's, never this request's candidate.
    token = str(room.get("callToken") or "")
    if front:  # lever 2 (router-latency-2026-11-08.md): the Router's session is warm before the caller's first line
        voice_calls.prewake_owner(owner_id, team=[front])
    allow, mode = cfg["allow"], call_mode(cfg, call)
    paused = mode != "live"
    return {
        "after": await latest_seq(call_id),  # the first `/events` cursor: a replayed start says nothing twice
        "mode": mode,
        "callId": call_id,
        "callToken": token,  # the worker sends it on every later route for this call (CALL_TOKEN_HEADER)
        "ownerId": owner_id,
        "businessName": business,
        "assistantName": assistant,
        "voice": voice_preferences.phone_voice(cfg.get("voice")),
        "spanish": cfg.get("spanish") is True,
        "greeting": greeting(cfg["greeting"], business, assistant),
        "facts": _facts(cfg) if allow["answer"] and not paused else "",
        "instructions": cfg["instructions"][:4000],
        "callerName": call["callerName"] if allow["greetByName"] else "",
        "callerMemory": call["callerMemory"],
        "can": {k: bool(allow[k]) and not paused for k in ("book", "move", "answer")},
        "transfer": bool(cfg["handoff"]["number"]),
        "forwardTo": forward_to,  # the worker transfers the caller there, then ends with forwarded: true
        "today": datetime.now(_tz(cfg)).strftime("%A %Y-%m-%d %H:%M"),
        # the number is a member's saved mobile with a PIN: the voice may say a PIN can be typed. Never proof.
        "ownerPin": bool(not forward_to and caller and await phone_pin.holders(owner_id, caller)),
    }


async def _live_slot(owner_id: str, line: str, call_id: str) -> str | None:
    """One of the line's LIVE_CALLS_PER_LINE slots for this call (end_call frees it; an unended call's expires)."""
    for i in range(LIVE_CALLS_PER_LINE):
        sk = f"VOICELIVE#{line}#{i}"
        if await messaging_repo.claim_voice_slot(owner_id, sk, call_id, LIVE_SLOT_S):
            return sk
    return None


# --- the owner calls in (board row 3.29): caller ID plus the PIN, then the orb's owner path --------------------


class PinIn(BaseModel):
    pin: Annotated[str, Field(pattern=r"^\d{4,8}$")]


async def owner_dm(ws: str, member: str) -> str:
    """The member's DM with the assistant, where the orb talks (found or created, as /voice/lightbulb opens it)."""
    front = await task_conversation._front_agent(ws)
    room = await messaging.create_room(
        AuthContext(user_id=member, org_id=ws),
        messaging.CreateRoomRequest(
            client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-owner-dm|{ws}|{member}|{front['id']}"),
            name=None,
            kind="dm",
            members=[messaging.CreateRoomMember(kind="agent", id=front["id"], role="member", listen_mode="active")],
        ),
    )
    return str(room.room_id)


@router.post("/internal/voice/calls/{call_id}/owner-pin")
async def owner_pin(call_id: str, body: PinIn) -> dict:
    """The digits the caller typed (DTMF, read by the worker; the model never sees them). Right, from the saved
    mobile of a member who set that PIN: the call becomes that member's owner mode, and `/turn` and `/events` use
    their DM from now on (`after` is its first cursor). Anything else: `{ok: false}`, the same for a wrong PIN, an
    unknown number or a lockout; `locked` says only that this call has no tries left."""
    meta, call = await _call(call_id)
    ws = meta["ownerId"]
    if call.get("ownerMode") and await _owner_seated(call_id, ws, call):
        return {"ok": True, "after": await latest_seq(call["dmRoomId"])}
    # the try is taken (atomically, before any hash is checked) or there is none left
    if call.get("status") != "live" or not await phone_pin.reserve_call_try(ws, call_id):
        return {"ok": False, "locked": True}
    member = await phone_pin.check(ws, call.get("caller") or "", body.pin)
    if member:
        try:
            dm = await owner_dm(ws, member)
        except (messaging.ApiProblem, task_conversation.NoFrontAgent):  # a removed member, or no assistant yet
            member = None
    if not member:
        await _patch_call(call_id, lambda c: {"steps": _step(c, "A phone PIN was entered and refused")})
        return {"ok": False, "locked": await phone_pin.call_tries(ws, call_id) >= phone_pin.CALL_TRIES}
    await _patch_call(call_id, lambda c: {"ownerMode": True, "ownerModeUserId": member, "dmRoomId": dm,
                                          "steps": _step(c, "The owner's PIN was right: owner mode")})  # fmt: skip
    return {"ok": True, "after": await latest_seq(dm)}


async def _owner_turn(call_id: str, ws: str, call: dict, body: "TurnIn") -> dict:
    """An owner-mode line: the member's own message in their DM, exactly as the orb posts it (voice_owner.turn), so
    the DM's Router decides it with the member as the principal. A line that answers the Worker question the voice
    just said answers it through the card's own path and wakes nobody (an approval stays card-only)."""
    member, dm, key = call["ownerModeUserId"], call["dmRoomId"], f"call-owner-turn:{call_id}:{body.n}"
    answered = await answer_said_question(ws, dm, member, body.answers, body.text, key)
    msg = await messaging.create_human_message(
        AuthContext(user_id=member, org_id=ws),
        dm,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            text=body.text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="voice",
        **({"wake": False} if answered else {}),
    )
    out = {"messageId": str(msg.message_id)}
    return {**out, "answered": body.answers.qid} if answered else out


async def _owner_seated(call_id: str, ws: str, call: dict) -> bool:
    """The owner-mode member is still a member seated in their DM (voice_owner._owner's check), re-read on every
    owner-mode turn and poll. Not any more: owner mode ends and the call is a caller's again."""
    members = await messaging_repo.list_members(call["dmRoomId"])
    if voice_calls._voice_requester(members, ws, call["ownerModeUserId"]):
        return True
    await _patch_call(
        call_id, lambda c: {"ownerMode": False, "steps": _step(c, "Owner mode ended: no longer a member")}
    )
    return False


def _is_probe(cfg: dict, body: StartIn) -> bool:
    """A call arriving within WINDOW_S of a pending forwarding check, from our own line or the business line (the
    carrier may present either as the caller)."""
    if body.callerNumber not in (body.calledNumber, _e164(str(cfg.get("forwardFrom") or ""))):
        return False
    return 0 <= voice_outbound.age_s(cfg.get("forwardingCheck")) <= voice_outbound.WINDOW_S


def greeting(custom: str, business: str, assistant: str) -> str:
    """The owner's greeting as written; ours only when they have none. The business owns its opening."""
    return custom.strip() or f"Hi, I'm {assistant}, {business}'s AI assistant. How can I help?"


class EndIn(BaseModel):
    durationSec: int = Field(ge=0, le=86400)
    summary: str = Field(default="", max_length=4000)
    transcript: list[dict] = Field(default=[], max_length=2000)
    silent: bool = False  # the worker hung up a line nobody spoke on ("are you still there?", then goodbye)
    forwarded: bool = False  # the run-out forward (start's forwardTo) connected; the assistant never answered


def outcome_of(call: dict) -> str:
    """One word for the Home row; needs_you and handoff_missed put a card in Needs you."""
    if call.get("handoffMissed"):
        return "handoff_missed"
    if call.get("work") or call.get("unknown"):
        return "needs_you"
    if call.get("bookings"):
        last = call["bookings"][-1]
        return "booked" if "newStart" not in last else "moved" if last["newStart"] else "cancelled"
    return "message" if call.get("messages") else "answered"


def transcript_lines(transcript: list[dict]) -> list[dict]:
    """The worker's lines as the record keeps them: `{who: caller|assistant, text, t}`; anyone else is dropped."""
    who = {"user": "caller", "caller": "caller", "assistant": "assistant"}
    lines = []
    for t in transcript:
        if str(t.get("role") or t.get("who")) not in who:
            continue
        line = {"who": who[str(t.get("role") or t.get("who"))], "text": str(t.get("text", ""))[:2000]}
        if isinstance(t.get("t"), (int, float)) and not isinstance(t["t"], bool):
            line["t"] = round(min(max(float(t["t"]), 0.0), 86400.0), 1)  # seconds since the call started
        lines.append(line)
    return lines


@router.post("/internal/voice/calls/{call_id}/end")
async def end_call(call_id: str, body: EndIn) -> dict:
    lines = transcript_lines(body.transcript)
    meta, _ = await _call(call_id)
    forwarded_to = (await agent_config(meta["ownerId"]))["handoff"]["number"] if body.forwarded else None
    before: dict = {}

    def change(call: dict) -> dict:
        before["status"] = call.get("status")
        if call.get("status") == "ended":
            return {}  # retries reuse the immutable first terminal result below
        # Billed time is never more than the server saw since start (M4): a worker cannot bill a 24-hour call.
        try:
            elapsed = int((datetime.now(_TZ) - datetime.fromisoformat(call["startedAt"])).total_seconds())
        except (KeyError, ValueError):
            elapsed = 0
        seconds = min(body.durationSec, max(elapsed, 0) + END_GRACE_S)
        if forwarded_to:
            return {"status": "ended", "endedAt": utc_now_iso(), "durationSec": seconds,
                    "outcome": "forwarded", "transferredTo": forwarded_to,
                    "steps": _step(call, "Minutes used up: forwarded to you")}  # fmt: skip
        return {
            "status": "ended",
            "endedAt": utc_now_iso(),
            "durationSec": seconds,
            "outcome": "silent" if body.silent and outcome_of(call) == "answered" else outcome_of(call),
            "summary": body.summary,
            "transcript": lines,
        }

    out = await _patch_call(call_id, change)
    if meta.get("liveSlot"):
        try:  # best-effort: the slot's lease expires on its own, and billing below must always run
            await messaging_repo.release_voice_slot(meta["ownerId"], meta["liveSlot"], call_id)
        except Exception:
            logger.warning("live slot release failed for %s", call_id, exc_info=True)
    # Every step below runs on a retried end too, each once: a first end that failed part-way finishes here.
    if not out.get("monthCounted"):

        def claim(c: dict) -> dict:  # at most once per call, whichever end claims it (counter failures are logged)
            before["claimed"] = not c.get("monthCounted")
            return {"monthCounted": True} if before["claimed"] else {}

        out = await _patch_call(call_id, claim)
        if before["claimed"]:
            try:  # the record is written: a counter failure must not 500 away caller memory, asks and the outcome
                await voice_billing.add_month_seconds(
                    meta["ownerId"], int(out["durationSec"]), datetime.fromisoformat(out["startedAt"])
                )
            except Exception:
                logger.warning("voice minutes counter failed for %s", call_id, exc_info=True)
    # This awaited write is the durable source + outbox admission. If it fails,
    # return an error so the worker retries; terminal retries keep the first
    # stored duration/start time rather than trusting a new request body.
    await voice_billing.report_minutes(meta["ownerId"], call_id, out["startedAt"], int(out["durationSec"]), "phone")
    seen = await _seen(meta["ownerId"], out["caller"]) if out.get("caller") else None
    if out.get("caller") and (seen or {}).get("lastCallId") != call_id:  # a retried end counts the visit once
        seen = seen or {}
        item = {**_caller_key(meta["ownerId"], out["caller"]), "lastCallAt": out["endedAt"], "lastCallId": call_id,
                "visits": int(seen.get("visits", 0)) + 1}  # fmt: skip
        if seen.get("forgottenAt"):
            item["forgottenAt"] = seen["forgottenAt"]
        if seen.get("forgottenAt") and datetime.fromisoformat(seen["forgottenAt"]) >= datetime.fromisoformat(
            out["startedAt"]
        ):
            item["facts"] = seen.get("facts", [])  # Forgotten during this call: write neither name nor facts
        else:
            item["facts"] = list(dict.fromkeys([*out.get("remembered", [])[::-1], *seen.get("facts", [])]))[:MAX_FACTS]
            if name := out.get("callerName") or real_name(seen.get("name")):
                item["name"] = name  # a placeholder kept before this fix is dropped here
        await run_in_thread(messaging_repo._table().put_item, Item=item)
    try:  # best-effort: the one outcome metric must never fail the route
        put_metric("VoiceCallOutcome", dimensions={"outcome": out["outcome"]})
    except Exception:
        logger.warning("VoiceCallOutcome metric failed", exc_info=True)
    if out["outcome"] not in NO_CALL_PUSH and meta.get("ownerUserId"):
        await _first_calls_push(meta["ownerUserId"], meta["ownerId"], call_id, out)
    asks = [("didnt_know", q) for q in dict.fromkeys(out.get("unknown", []))]
    if out.get("handoffMissed"):
        asks.append(("handoff", ""))  # nobody free or the transfer failed: line_problem is for system issues
    for kind, question in asks if meta.get("ownerUserId") else ():
        ask = call_ask(call_id, out, kind, question)
        await _start_task(call_id, meta, out, ask["title"], f"{ask['title']}\n{ask['text']}", ask=ask)
    return out


# After Go live the owner hears about the first answered calls, so they see it working; then no more.
# Cards (needs_you, handoff_missed) push on their own, silent/forwarded calls were not answered.
NO_CALL_PUSH = {"silent", "forwarded", "needs_you", "handoff_missed"}
CALL_PUSHES = 3


async def _first_calls_push(user_id: str, ws: str, call_id: str, call: dict) -> None:
    """One conditional write both admits a call to the first three and dedupes a retried end (the id is in the set)."""
    try:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key={"pk": messaging_repo.owner_pk(ws), "sk": "CALLPUSH"},
            UpdateExpression="ADD calls :c",
            ConditionExpression="attribute_not_exists(calls) OR (size(calls) < :n AND NOT contains(calls, :id))",
            ExpressionAttributeValues={":c": {call_id}, ":n": CALL_PUSHES, ":id": call_id},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("call push claim failed for %s", call_id, exc_info=True)
        return
    who = first_name(call)
    try:
        await push_service.send_push_to_user(
            user_id, "Your assistant answered a call", f"{who} called." if who else "Someone called.",
            {"source": "voice_call", "callId": call_id},
        )  # fmt: skip
    except Exception:  # best-effort: send_push_to_user already swallows, this is belt and braces
        logger.warning("call push failed for %s", call_id, exc_info=True)


# --- Needs you: what a person must decide after a call (Questions on a per-call task) ------------------------

CALL_OPTIONS = {
    "didnt_know": [("answer", "Answer"), ("call_myself", "I'll call them"), ("done", "Done")],
    "handoff": [("call_myself", "I'll call them"), ("done", "Done")],
    "line_problem": [("call_myself", "I'll call them"), ("done", "Done")],
}
KNOWLEDGE_SOURCE = "you answered this in Needs you"


def call_ask(call_id: str, call: dict, kind: str, question: str = "") -> dict:
    """`task_asks.open_ask`'s arguments for one call Question (a blocking `question`, subkind = the card's kind).
    Its qid is the call, kind and question, so a retried end opens it once."""
    who = call.get("callerName") or call.get("caller") or "a caller"
    title = {"didnt_know": f"Answer {who}: {question}", "handoff": f"{who} wanted a person",
             "line_problem": f"{who}'s call hit a problem on the line"}[kind]  # fmt: skip
    return {
        "qid": questions.qid_for(f"{call_id}:{kind}:{question}"),
        "kind": "question",
        "subkind": kind,
        "title": title[:200],
        "text": f"Caller: {call.get('caller') or 'unknown number'}",
        "options": [{"id": i, "label": label} for i, label in CALL_OPTIONS[kind]],
        "blocking": True,
        "attempt": None,
        "run_id": None,
        "preview": {
            "kind": kind,
            "roomId": call_id,
            "caller": call.get("caller", ""),
            "question": question,
            "firstName": first_name(call),
        },
    }


async def remember_answer(owner_id: str, question: str, answer: str, by: str) -> None:
    """An answered "didn't know" goes into the Agent page's Knowledge and leaves `toAdd` (any answerer, no admin
    save). Idempotent. ponytail: read-modify-write like PUT /voice/agent; an admin's save at the same moment can
    drop it (a revision check on the document fixes both)."""
    cfg = await messaging_repo.get_voice_agent(owner_id)
    if cfg is None:
        return  # no Agent page yet: writing one would skip onboarding
    item = {"q": question, "a": answer, "source": KNOWLEDGE_SOURCE}
    knowledge, to_add = list(cfg.get("knowledge") or []), list(cfg.get("toAdd") or [])
    if item in knowledge and question[:TO_ADD_CHARS] not in to_add:
        return
    await messaging_repo.put_voice_agent(
        owner_id,
        {
            **cfg,
            "knowledge": knowledge if item in knowledge else [*knowledge, item],
            "toAdd": [t for t in to_add if t != question[:TO_ADD_CHARS]],
        },
        by,
    )


MAX_FACTS = 10
TO_ADD_MAX, TO_ADD_CHARS = 20, 200  # the Agent page's "questions to add": caller text, so capped (oldest drop)


def _to_add(cfg: dict, item: str) -> list[str]:
    return list(dict.fromkeys([*(cfg.get("toAdd") or []), item[:TO_ADD_CHARS]]))[-TO_ADD_MAX:]


class RememberIn(BaseModel):
    fact: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Literal["caller", "business"] = "caller"


@router.post("/internal/voice/calls/{call_id}/remember")
async def remember(call_id: str, body: RememberIn) -> dict:
    """A caller's own preference (kept on their row at the end of the call) or a fact about the business (cfg toAdd)."""
    meta, call = await _call(call_id)
    if call_mode(await agent_config(meta["ownerId"]), call) != "live":
        raise _err(403, "not_allowed")
    if re.search(r"\d{12,}", re.sub(r"\D", "", body.fact)):  # card-like: never stored
        raise _err(400, "looks_like_a_card")
    if body.kind == "business":
        cfg = await messaging_repo.get_voice_agent(meta["ownerId"]) or {}
        await messaging_repo.put_voice_agent(meta["ownerId"], {**cfg, "toAdd": _to_add(cfg, body.fact)}, "voice")
    else:
        await _patch_call(call_id, lambda c: {"remembered": [*c.get("remembered", []), body.fact][-MAX_FACTS:]})
    return {"remembered": True}


async def resolve_call(owner_id: str, call_id: str, by: str) -> None:
    """A call's Question was answered: the call is handled (what POST /voice/calls/{id}/done writes), so Home's
    "Needs you" stops listing it. ponytail: the first answered Question resolves a call that had several."""
    meta = await messaging_repo.get_room(call_id)
    if not meta or meta.get("ownerId") != owner_id or meta.get("kind") != "call":
        return
    await _patch_call(call_id, lambda c: {} if c.get("resolvedAt") else {"resolvedAt": utc_now_iso(), "resolvedBy": by})


# --- the assistant's own calendar (spec §4.1b: a hold in its calendar when the business has no API) --------


def _minutes(cfg: dict, service: str) -> int:
    s = service.strip().lower()
    for x in cfg["services"]:
        n = str(x.get("name", "")).strip().lower()
        if n and (n in s or s in n):
            return int(x.get("minutes") or 60)
    return 60


def _closed_on(cfg: dict, date: str) -> bool:
    """True when `date` (local "YYYY-MM-DD") falls in a closure range. ISO dates compare as strings; anything that is
    not a list of {from, to} ranges (the old free-text value) closes nothing."""
    h = cfg.get("holidays")
    return isinstance(h, list) and any(
        isinstance(r, dict)
        and isinstance(r.get("from"), str)
        and r["from"]
        and r["from"] <= date <= str(r.get("to") or r["from"])
        for r in h
    )


def _open_window(cfg: dict, date: str) -> tuple[datetime, datetime] | None:
    if _closed_on(cfg, date):
        return None
    h = cfg["hours"].get(_DAYS[datetime.strptime(date, "%Y-%m-%d").weekday()]) or {}
    if not h or h.get("closed"):
        return None
    return (datetime.strptime(f"{date}T{h['open']}", "%Y-%m-%dT%H:%M"),
            datetime.strptime(f"{date}T{h['close']}", "%Y-%m-%dT%H:%M"))  # fmt: skip


def free_slots(cfg: dict, date: str, taken: list[tuple[datetime, datetime]], now: datetime, minutes: int) -> list[str]:
    """Every half-hour start on `date` (local "YYYY-MM-DD") where `minutes` fits inside opening hours, after now,
    with `bufferMin` clear of every booking in `taken`."""
    window = _open_window(cfg, date)
    if not window:
        return []
    at, close = window
    need, buf = timedelta(minutes=minutes), timedelta(minutes=int(cfg.get("bufferMin") or 0))
    out = []
    while at + need <= close:
        if at > now and all(at + need + buf <= s or e + buf <= at for s, e in taken):
            out.append(at.strftime("%Y-%m-%dT%H:%M"))
        at += _GRID
    return out


async def _booking_rows(owner_id: str, prefix: str) -> list[dict]:
    args = {
        "KeyConditionExpression": Key("pk").eq(messaging_repo.owner_pk(owner_id)) & Key("sk").begins_with(prefix),
        "ConsistentRead": True,
    }
    rows = []
    while True:
        result = await run_in_thread(messaging_repo._table().query, **args)
        rows.extend(result.get("Items", []))
        if not result.get("LastEvaluatedKey"):
            return rows
        args["ExclusiveStartKey"] = result["LastEvaluatedKey"]


async def _bookings(owner_id: str, prefix: str) -> list[dict]:
    return await _booking_rows(owner_id, f"VOICEBOOK#{prefix}")


async def _taken(owner_id: str, date: str) -> list[tuple[datetime, datetime]]:
    # Provisional move targets stay visible, including moves to a different date.
    holds = await _booking_rows(owner_id, f"VOICEHOLD#{date}")
    out = []
    for b in [*(await _bookings(owner_id, date)), *holds]:
        start = b.get("start") or b["sk"].removeprefix("VOICEBOOK#")
        s = datetime.strptime(start, "%Y-%m-%dT%H:%M")
        out.append((s, s + timedelta(minutes=int(b.get("minutes") or 60))))
    return out


@functools.cache
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # a bad stored name must not 500 every call
        logger.warning("unknown business timezone %r; using America/New_York", name)
        return _TZ


def _tz(cfg: dict) -> ZoneInfo:
    return _zone(cfg.get("timezone") or "America/New_York")


def _now(cfg: dict) -> datetime:
    return datetime.now(_tz(cfg)).replace(tzinfo=None)


def _cells(cfg: dict, start: str, minutes: int) -> list[str]:
    """The 5-minute UTC cells a booking holds: [start, start + minutes + bufferMin)."""
    t = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=_tz(cfg)).astimezone(ZoneInfo("UTC"))
    n = -(-(minutes + int(cfg.get("bufferMin") or 0)) // 5)
    return [f"VOICECELL#{(t + timedelta(minutes=5 * i)):%Y-%m-%dT%H:%M}" for i in range(n)]


class DateIn(BaseModel):
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    service: str = Field(default="", max_length=200)


@router.post("/internal/voice/calls/{call_id}/availability")
async def availability(call_id: str, body: DateIn) -> dict:
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "book", "move")
    slots = await voice_booking.free(meta["ownerId"], cfg, call_id, body.date, _minutes(cfg, body.service))
    return {"date": body.date, "slots": slots}


class BookIn(BaseModel):
    start: _Local
    service: _Str
    name: _Str


async def _book(owner_id: str, call_id: str, call: dict, cfg: dict, start: str, service: str, name: str,
                old: dict | None = None) -> None:  # fmt: skip
    """Write the booking and its cells in one TransactWriteItems with `attribute_not_exists` on every cell, so an
    overlap fails atomically (https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html,
    https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html). `old` is the booking
    being moved: its row and cells go in the same transaction."""
    minutes, pk, table = (
        _minutes(cfg, service),
        messaging_repo.owner_pk(owner_id),
        messaging_repo.settings.MESSAGING_TABLE,
    )
    cells = _cells(cfg, start, minutes)
    if len(cells) > 99:  # DynamoDB caps a transaction at 100 items (the booking row is one)
        raise _err(400, "booking_too_long")
    if start not in free_slots(cfg, start[:10], [], _now(cfg), minutes) or (old and old["sk"] == f"VOICEBOOK#{start}"):
        raise _err(400, "slot_unavailable")
    sk, mine = f"VOICEBOOK#{start}", set(old.get("cells", [])) if old else set()
    items = [{"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(sk)", "Item": {
        "pk": pk, "sk": sk, "bookingId": (old or {}).get("bookingId") or str(uuid.uuid4()),
        "generation": int((old or {}).get("generation", 0)) + 1,
        "bookingProvider": (old or {}).get("bookingProvider") or ("google" if cfg.get("bookingsGo") == "google" else "own"),
        "bookingState": "held" if cfg.get("bookingsGo") == "google" else "confirmed",
        "service": service, "minutes": minutes, "name": name, "phone": call.get("caller", ""),
        "callId": call_id, "createdAt": utc_now_iso(), "cells": cells}}}]  # fmt: skip
    for c in cells:
        put = {
            "TableName": table,
            "Item": {
                "pk": pk,
                "sk": c,
                "booking": sk,
                "bookingId": items[0]["Put"]["Item"]["bookingId"],
                "expiresAt": _cell_expiry(c),
            },
            "ConditionExpression": "attribute_not_exists(sk)",
        }
        if c in mine:  # the booking being moved holds it now and gives it up in this same transaction
            put |= {
                "ConditionExpression": "attribute_not_exists(sk) OR booking = :old",
                "ExpressionAttributeValues": {":old": old["sk"]},
            }
        items.append({"Put": put})
    if old:
        items += _release(table, pk, old, keep=set(cells))
    if len(items) > 100:
        raise _err(400, "booking_too_long")
    try:
        await messaging_repo._transact(items)
    except ClientError as exc:
        if messaging_repo.is_cancelled(exc):
            raise _err(409, "slot_taken") from exc
        raise


def _cell_expiry(cell: str) -> int:  # the table's TTL attribute: the cell's end + 1 day
    t = datetime.strptime(cell.removeprefix("VOICECELL#"), "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("UTC"))
    return int(t.timestamp()) + 300 + 86400


def _release(table: str, pk: str, row: dict, keep: set[str] = frozenset()) -> list[dict]:
    # ponytail: VOICEBOOK# rows made before cells existed have no "cells" and hold nothing; no migration, they age out.
    out = []
    for key in [row["sk"], *row.get("cells", [])]:
        if key in keep:
            continue
        if key == row["sk"]:
            condition, values = voice_booking.row_condition(row)
        elif row.get("bookingId"):
            # A cell the table's TTL already removed is released; one that exists must still be this booking's.
            condition, values = (
                "attribute_not_exists(sk) OR (bookingId = :bid AND booking = :booking)",
                {":bid": row["bookingId"], ":booking": row["sk"]},
            )
        else:
            condition, values = "attribute_not_exists(sk) OR (booking = :booking)", {":booking": row["sk"]}
        out.append(
            {
                "Delete": {
                    "TableName": table,
                    "Key": {"pk": pk, "sk": key},
                    "ConditionExpression": condition,
                    "ExpressionAttributeValues": values,
                }
            }
        )
    return out


@router.post("/internal/voice/calls/{call_id}/book")
async def book(call_id: str, body: BookIn) -> dict:
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "book")
    counter = f"VOICEBOOKS#{_day()}#{call.get('caller') or 'withheld'}"
    # One unit per reservation, keyed by its start: a retry that returns or reconciles that booking ("had") is never
    # a second unit, so the cap cannot lock a pending booking out of its own reconciliation.
    unit = await messaging_repo.take_voice_unit(meta["ownerId"], counter, body.start, BOOKINGS_PER_CALLER_PER_DAY)
    if unit is None:
        await _limit(call_id, "too many bookings from this number today")
        raise _err(403, "not_allowed")
    try:
        result = await voice_booking.book(meta["ownerId"], cfg, call_id, call, body.start, body.service, body.name)
    except BaseException:
        if unit == "new":
            await messaging_repo.give_back_voice_unit(meta["ownerId"], counter, body.start)  # nothing was reserved
        raise
    if unit == "new" and result["state"] not in ("confirmed", "pending", "held"):  # nothing was reserved
        await messaging_repo.give_back_voice_unit(meta["ownerId"], counter, body.start)
    if result["state"] != "confirmed":
        await _patch_call(call_id, lambda c: {"steps": _step(c, f"Booking not confirmed: {result['code']}")})
        return {"booked": False, **result}
    entry = {"start": body.start, "service": body.service, "name": body.name}
    line = f"{body.service} · {_pretty(body.start)} for {body.name}"
    await _patch_call(call_id, lambda c: {"bookings": [*c.get("bookings", []), entry], "booking": line,
                                          "callerName": c.get("callerName") or body.name,
                                          "steps": _step(c, f"Booked {line}")})  # fmt: skip
    return {"booked": True, **entry}


class ChangeIn(BaseModel):
    start: _Local
    newStart: _Local | None = None  # None cancels


@router.post("/internal/voice/calls/{call_id}/change")
async def change_booking(call_id: str, body: ChangeIn) -> dict:
    """Move or cancel a booking -- only one made from this caller's number (a contact never touches another's)."""
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "move")
    owner_pk, sk = messaging_repo.owner_pk(meta["ownerId"]), f"VOICEBOOK#{body.start}"
    row = await messaging_repo._get(owner_pk, sk)
    if not row or not call.get("caller") or row.get("phone") != call["caller"]:
        raise _err(404, "booking_not_found")
    if body.newStart:
        result = await voice_booking.move(meta["ownerId"], cfg, call_id, call, row, body.newStart)
    else:
        result = await voice_booking.cancel(meta["ownerId"], cfg, call_id, row)
    if result["state"] != "confirmed":
        await _patch_call(call_id, lambda c: {"steps": _step(c, f"Booking change not confirmed: {result['code']}")})
        return {"moved" if body.newStart else "cancelled": False, **result}
    entry = {"start": body.start, "newStart": body.newStart, "service": row["service"], "name": row["name"]}
    line = (
        f"{row['service']} moved from {_pretty(body.start)} to {_pretty(body.newStart)}"
        if body.newStart
        else f"{row['service']} on {_pretty(body.start)} cancelled"
    )
    await _patch_call(call_id, lambda c: {"bookings": [*c.get("bookings", []), entry], "booking": line,
                                          "steps": _step(c, line[0].upper() + line[1:])})  # fmt: skip
    return {"moved" if body.newStart else "cancelled": True, **entry}


@router.post("/internal/voice/calls/{call_id}/my-bookings")
async def my_bookings(call_id: str) -> dict:
    meta, call = await _call(call_id)
    _allowed(await agent_config(meta["ownerId"]), call, "move")
    caller = call.get("caller")
    rows = [b for b in await _bookings(meta["ownerId"], "") if caller and b.get("phone") == caller]

    def public_booking(b: dict) -> dict:
        out = {"start": b["sk"].removeprefix("VOICEBOOK#"), "service": b["service"]}
        if b.get("pending") or b.get("bookingState") == "held":
            out |= {
                "state": "pending" if b.get("pending") else "held",
                "confirmed": False,
                "pendingAction": (b.get("pending") or {}).get("action"),
                "requestedStart": (b.get("pending") or {}).get("newStart"),
            }
        return out

    return {"bookings": [public_booking(b) for b in rows]}


# --- message, didn't know, transfer, start_work -----------------------------------------------------------


class MessageIn(BaseModel):
    name: str = Field(default="", max_length=200)
    text: _Str
    callbackNumber: str = Field(default="", max_length=40)


@router.post("/internal/voice/calls/{call_id}/message")
async def take_message(call_id: str, body: MessageIn) -> dict:
    """Always open, paused or not: a message is what every refused tool falls back to."""
    await _call(call_id)
    entry = {**body.model_dump(), "name": real_name(body.name), "at": utc_now_iso()}
    await _patch_call(call_id, lambda c: {"messages": [*c.get("messages", []), entry],
                                          "callerName": c.get("callerName") or body.name,
                                          "steps": _step(c, f"Took a message: {body.text[:160]}")})  # fmt: skip
    return {"taken": True}


class UnknownIn(BaseModel):
    question: _Str


@router.post("/internal/voice/calls/{call_id}/unknown")
async def didnt_know(call_id: str, body: UnknownIn) -> dict:
    """A question the assistant couldn't answer: the call lands in Needs you."""
    meta, _ = await _call(call_id)
    body = UnknownIn(question=redact_cards(body.question))  # it also lands in the Agent page's toAdd
    await _patch_call(call_id, lambda c: {"unknown": [*c.get("unknown", []), body.question],
                                          "steps": _step(c, f"Didn't know: {body.question[:160]}")})  # fmt: skip
    cfg = await messaging_repo.get_voice_agent(meta["ownerId"])
    if cfg is not None and body.question[:TO_ADD_CHARS] not in (cfg.get("toAdd") or []):
        await messaging_repo.put_voice_agent(
            meta["ownerId"], {**cfg, "toAdd": _to_add(cfg, body.question)}, meta.get("ownerUserId") or ""
        )
    return {"noted": True}


class TransferIn(BaseModel):
    failed: bool = False  # the worker's SIP transfer didn't connect


@router.post("/internal/voice/calls/{call_id}/transfer")
async def transfer(call_id: str, body: TransferIn | None = None) -> dict:
    """The number to hand the caller to (Agent page `handoff`), or none: after hours, unset, or a failed transfer
    is `handoff_missed`. The worker does the SIP transfer."""
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    if call_mode(cfg, call) != "live":
        raise _err(403, "not_allowed")
    h, now = cfg["handoff"], _now(cfg)
    window = _open_window(cfg, now.strftime("%Y-%m-%d"))
    is_open = bool(window) and window[0] <= now < window[1]
    to = us_number(str(h["number"] or ""))  # a number saved before the US/CA check is never dialed (M2)
    if (body and body.failed) or not to or (h["onlyOpenHours"] and not is_open):
        why = "the transfer didn't connect" if body and body.failed else "nobody was available"
        await _patch_call(call_id, lambda c: {"handoffMissed": True, "steps": _step(c, f"Asked for a person; {why}")})
        return {"transferTo": None}
    await _patch_call(call_id, lambda c: {"transferredTo": to, "steps": _step(c, "Transferred to a person")})
    return {"transferTo": to}


async def _start_task(call_id: str, meta: dict, call: dict, title: str, brief: str, ask: dict | None = None) -> dict:
    """A Worker task for this call (the existing voice-start path), `origin: phone_contact`, requested by the line's
    owner user (written by start_call, never the model). The same title in one call is the same task. `ask`: the
    task opens waiting on that Question instead of running first (call_ask)."""
    requester = meta.get("ownerUserId")
    if not requester:
        raise _err(403, "no_requester")
    run_id = hashlib.sha256(f"{call_id}:{title.strip().lower()}".encode()).hexdigest()[:32]
    await messaging_repo.put_voice_run(
        meta["ownerId"], run_id, {"roomId": call_id, "requesterUserId": requester, "agentId": None}
    )
    brief = f"{CONTACT_NOTE}\nCaller: {call.get('caller') or 'unknown'}\n\n{brief}"
    return await start_voice_work(
        meta["ownerId"],
        VoiceStartIn(run_id=run_id, call_id=call_id, room_id=call_id, title=title, brief=brief),
        origin="phone_contact",
        **({"ask": ask} if ask else {}),
    )


class WorkIn(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    brief: Annotated[str, Field(min_length=1, max_length=8000)]


@router.post("/internal/voice/calls/{call_id}/start-work", status_code=202)
async def start_work(call_id: str, body: WorkIn) -> dict:
    """Open-ended work becomes a Worker task (the existing voice-start path), marked `origin: phone_contact` so
    effect_check asks the owner before any outside action. The requester is the line's owner user, written by the
    backend; the same title in one call is the same task (a retried tool call)."""
    meta, call = await _call(call_id)
    if call_mode(await agent_config(meta["ownerId"]), call) != "live":
        raise _err(403, "not_allowed")
    out = await _start_task(call_id, meta, call, body.title, body.brief)
    await _patch_call(call_id, lambda c: {"work": [*c.get("work", []), {"title": body.title, **out}],
                                          "steps": _step(c, f"Handed to the team: {body.title}")})  # fmt: skip
    return out


# --- the caller's lines go to the Router (one-brain plan, "Phone, too", founder 8 Nov 2026) ------------------
#: Said to the Router with each line: who is speaking, and that the reply is spoken on the phone.
CALLER_HINT = (
    "(Said by a caller on the business's phone line, an outside contact, not a member. The reply is read out on "
    "the call: one or two short spoken sentences, no lists, links or markdown. Offer at most three open times.)"
)


class TurnIn(BaseModel):
    n: int = Field(ge=0, le=100000)  # the line's place in the call: with the call id, its one message and trigger
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    answers: QuestionRef | None = None  # owner mode: the Worker question the voice said last (voice_owner.TurnIn)


@router.post("/internal/voice/calls/{call_id}/turn")
async def caller_turn(call_id: str, body: TurnIn) -> dict:
    """One delegated caller line: a message in the call's room, authored by the caller (a non-member: no admission
    row, no memory), and a `phone_contact` trigger for that room, so the Router decides it with the caller's scope
    and the Business profile, and its Front desk threads run as the caller. Keyed on (call, n): a retry is the same."""
    meta, call = await _call(call_id)
    body = body.model_copy(update={"text": redact_cards(body.text)})  # the room message and the Router's trigger
    # the PIN was right (owner_pin) and the member is still seated: the orb's owner path, not the caller's
    ended: dict = {}
    if call.get("ownerMode"):
        if await _owner_seated(call_id, meta["ownerId"], call):
            return await _owner_turn(call_id, meta["ownerId"], call, body)
        # removed mid-call, found here before any poll: this answer carries the transition `/events` would have
        # (the worker's cursor goes back to the call room, just before this line)
        ended = {"ownerModeEnded": True, "after": await latest_seq(call_id)}
    ws, requester = meta["ownerId"], meta.get("ownerUserId")
    if not requester:
        raise _err(403, "no_requester")
    if call_mode(await agent_config(ws), call) != "live":
        raise _err(403, "not_allowed")  # a paused line only takes messages (/message)
    if not await messaging_repo.take_voice_unit(ws, f"VOICETURNS#{call_id}", str(body.n), TURNS_PER_CALL):
        await _limit(call_id, f"more than {TURNS_PER_CALL} lines to the team")
        raise _err(403, "not_allowed")  # the worker takes it as a message instead
    who = f"Caller {call.get('callerName') or call.get('caller') or ''}".strip()
    key = f"call-turn:{call_id}:{body.n}"
    msg, _ = await messaging_repo.create_message(
        room_id=call_id,
        owner_id=ws,
        idemp_pk=messaging_repo.owner_pk(ws),
        idemp_sk=messaging_repo.agent_idemp_sk(key),
        req_hash=messaging_repo.request_hash(call_id, body.n, body.text),
        author={"kind": "human", "id": f"caller:{call_id}", "displayName": who},
        text=body.text,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=None,
        client_message_id=None,
        silent=True,
        author_member_check=False,
        via="voice",
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=ws,
        trigger_id=ts.trigger_id("voice", call_id, f"turn-{body.n}"),
        source="voice",
        admission="respond",
        lane="shared",  # the batch key is the call's room (trigger_scope): a call never shares a Router prompt
        actor_user_id=requester,  # the line's owner user, as start_work's tasks: written by start_call, never the model
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "roomId": call_id,
            "messageId": msg["messageId"],
            "callId": call_id,
            "origin": "phone_contact",
            "from": who,
            "text": f"{body.text}\n\n{CALLER_HINT}",
        },
    )
    if task_id := await _open_thread(ws, call_id, int(msg["messageSeq"])):
        # The call's open thread takes the line, as a typed reply in a task's thread does (admission's
        # pass-through): it already holds the times it offered and the caller's answers (8 Nov prod final smoke:
        # each line started a new thread, and the "yes" thread asked again and picked a time never offered).
        trig = {**trigger.model_dump(by_alias=True), "routedTo": {"taskId": task_id}}
        trig["ref"]["deliverRoomId"] = call_id
        await tasks_repo.put_trigger(ws, trig)
        if blocked := await task_conversation.gate_input(ws, task_id, trig):  # §14.10, as admission's pass-through
            await task_conversation.post_as_lightbulb(
                ws,
                call_id,
                text=blocked,
                event_id=f"task-gate:{task_id}:{trigger.trigger_id}",
                remember=False,
                in_reply_to=msg["messageId"],
                attrs={"voiceWaiting": False},
            )
            task_id = None
        else:
            await admission._to_task(ws, task_id, trigger.trigger_id)
    else:
        await admission.enqueue_trigger(ws, trigger, room_id=call_id)
    return {"messageId": msg["messageId"], "triggerId": trigger.trigger_id, **({"taskId": task_id} if task_id else {}),
            **ended}  # fmt: skip


_CLOSED = ("resolved", "failed")


async def _open_thread(ws: str, call_id: str, before_seq: int) -> str | None:
    """The call's latest caller thread still open (its card on a Lightbulb line in the call room), else None: a
    call's first line, or a line after its thread failed or was closed, goes to the Router."""
    for row in reversed(await messaging_repo.list_messages_before(call_id, before_seq, 50)):
        for ref in reversed(row.get("taskRefs") or []):
            task = await tasks_repo.get_task(ws, ref.get("taskId") or "")
            if (
                task
                and task.get("origin") == "phone_contact"
                and (task.get("audience") or {}).get("roomId") == call_id  # this call's, never another call's
                and task.get("status") not in _CLOSED
            ):
                return task["taskId"]
    return None


async def after_call(room_id: str, item: dict) -> None:
    """A team answer that lands in a call's room after the caller hung up (8 Nov prod re-run #3: the booking
    confirmation and the card answer, 8-10 s after hang-up): kept on the call record (`afterCall`, and a step the
    Home row shows) so the owner sees what the caller never heard. Thread and progress lines are not answers."""
    if item.get("progress") or item.get("threadRootMessageId") or not (text := (item.get("text") or "").strip()):
        return
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("kind") != "call" or (meta.get("call") or {}).get("status") != "ended":
        return
    await _patch_call(
        room_id,
        lambda c: {
            "afterCall": [*c.get("afterCall", []), {"messageId": item.get("messageId"), "text": text[:2000]}],
            "steps": _step(c, f"After the call, not heard by the caller: {text[:160]}"),
        },
    )


class CallEventsIn(BaseModel):
    after: int = Field(ge=0)


@router.post("/internal/voice/calls/{call_id}/events")
async def call_events(call_id: str, body: CallEventsIn) -> dict:
    """Lightbulb's messages in the call's room after seq `after` (the Router's ack and replies, the Front desk's
    answers), oldest first: the phone voice says each once (huddle-agent mouth.py)."""
    meta, call = await _call(call_id)
    if call.get("ownerMode"):  # the member's DM, as the orb's /events: the cursor is the DM's (owner_pin's after)
        if await _owner_seated(call_id, meta["ownerId"], call):
            return await agent_lines(call["dmRoomId"], body.after)
        # removed mid-call: nothing from the DM; the worker goes back to the call room's cursor and the caller prompt
        return {"messages": [], "after": await latest_seq(call_id), "ownerModeEnded": True}
    out = await agent_lines(call_id, body.after)
    claimed = [m for m in out["messages"] if BOOKING_NOW.search(_plain(m["text"]))]
    if claimed and not call.get("bookings") and not await _has_upcoming(meta["ownerId"], call.get("caller") or ""):
        for m in claimed:
            m["text"] = " ".join(
                NOT_BOOKED if BOOKING_NOW.search(_plain(s)) else s for s in re.split(r"(?<=[.?!])\s+", m["text"])
            )
    return out


#: A line that says a booking is being made right now. Only `/book` and `/change` (the Front desk's booking tools)
#: put one on the call's record; until then, and when the caller has no upcoming booking, that sentence is not said
#: (8 Nov prod final smoke: "Vera is booking your founding team setup for Saturday, November 14 at 12:00 PM" was
#: said, and nothing was ever booked). "You're booked Tuesday at 3 PM" (a booking from an earlier call, being moved)
#: and "That 2 PM slot is booked" are not claims of this kind.
BOOKING_NOW = re.compile(r"\b(is booking|are booking|will book|i'?ll book|i'?ve booked|you'?re (all )?set)\b", re.I)


def _plain(text: str) -> str:
    return text.replace("\u2019", "'")


async def _has_upcoming(ws: str, caller: str) -> bool:
    """The caller has a booking from now on (the one `business_profile` lists for them)."""
    if not caller:
        return False
    from core.services import business_profile, voice_booking

    now = f"{_now(await agent_config(ws)):%Y-%m-%dT%H:%M}"
    return any(
        b.get("phone") == caller and voice_booking.confirmed(b) for b in await business_profile._upcoming(ws, now)
    )


NOT_BOOKED = "The team is still working on that and hasn't made the booking yet."
