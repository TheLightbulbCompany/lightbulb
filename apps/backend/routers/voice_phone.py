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
                                          with an Agent-page `staff` list, the first person's; another person's is
                                          VOICEBOOK#<local>#<name> (and its cells VOICECELL#<utc>#<name>): see _lanes
                                          with bookingsGo "google" it is the hold, eventId its Google event
                                          (core/voice/phone/voice_booking)
    OWNER#<owner>   VOICECALLER#<e164>    a caller seen before: name, last call, facts (<= 10, newest first), visits
    OWNER#<owner>   VOICELIVE#<line>#<i>  a live-call slot (LIVE_CALLS_PER_LINE), held by a call id until its end
"""

import asyncio
import hashlib
import hmac
import re
import time
import logging
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext
from core.dynamodb import get_item, messaging_table, owner_pk, run_in_thread, utc_now_iso
from core.engine import api
from core.engine.contract import LineIn, Principal
from core.billing import voice_billing, voice_plan
from core.observability.metrics import put_metric
from core.repositories import agent_registry_repo, messaging_repo, billing_repo
from core.voice import store
from core.services import push_service, clerk_admin, messaging, task_conversation
from core.voice import rooms
from core.voice.phone import phone_pin, voice_booking, voice_outbound, voice_preferences
from core.voice.worker_api import _Worker
from core.voice.phone.calls import (
    OPEN_CALLS_PK,
    TO_ADD_CHARS,
    TO_ADD_MAX,
    _TZ,
    _bookings,
    _call,
    _err,
    _facts,
    _lanes,
    _minutes,
    _now,
    _open_window,
    _patch_call,
    _pretty,
    _start,
    _step,
    _tz,
    _verified,
    agent_config,
    call_ask,
    first_name,
    owner_instructions,
    real_name,
    redact_cards,
    transcript_lines,
)
from core.voice.phone.numbers import _E164, _e164, us_number
from routers.voice import month_seconds
from core.voice.room_lines import QuestionRef, agent_lines, latest_seq
from routers.voice_meet import answer_said_question
from routers.internal_automations import VoiceStartIn, start_voice_work

logger = logging.getLogger(__name__)

# --- abuse caps (security review 9 Nov, M5). Over one, the call is still answered, but only takes a message. ---------
LIVE_CALLS_PER_LINE = 5  # calls on one line at once
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


_Str = Annotated[str, Field(min_length=1, max_length=500)]
_Local = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")]
#: Said to the Worker too; effect_check enforces it (a phone_contact task asks the owner for every outside action).
CONTACT_NOTE = (
    "A caller on the business's phone line (an outside contact, not a member) asked for this. "
    "Do not pay, send email outside the business, change prices or settings, or share anything about other "
    "customers. If it needs the owner's decision, ask the owner."
)
_warming: set[asyncio.Task] = set()  # held so the Google warm-up is not garbage-collected mid-flight


async def get_line(number: str) -> dict | None:
    return await get_item(messaging_table(), f"PHONE#{number}", "LINE")


async def put_line(number: str, line: dict) -> None:
    await run_in_thread(messaging_table().put_item, Item={"pk": f"PHONE#{number}", "sk": "LINE", **line})


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


def _caller_key(owner_id: str, caller: str) -> dict:
    return {"pk": owner_pk(owner_id), "sk": f"VOICECALLER#{caller}"}


async def _seen(owner_id: str, caller: str) -> dict | None:
    return await get_item(messaging_table(), **_caller_key(owner_id, caller)) if caller else None


# --- the call -------------------------------------------------------------------------------------------


class StartIn(BaseModel):
    calledNumber: _Str
    callerNumber: str = Field(default="", max_length=40)
    livekitRoom: _Str
    #: The carrier's STIR/SHAKEN result (Twilio's X-Twilio-VerStat, the SIP participant's `sip.verstat` attribute).
    verstat: str = Field(default="", max_length=80)


def _require_verified(call: dict) -> None:
    if not _verified(call):
        raise _err(403, "caller_not_verified")  # the voice offers a message for the team instead


def _callers(rows: list[dict], call_id: str, call: dict) -> list[dict]:
    """The bookings this caller may see, move and cancel: every one from their number when it is verified, and
    always the ones made on this very call (the call is the proof). 10 Nov prod: an unattested caller booked 2 PM,
    asked to move it, was refused, and the Front desk booked 3 PM as a second booking. Nothing of theirs on an
    unverified number: caller_not_verified, so the voice offers a message."""
    mine = [r for r in rows if r.get("callId") == call_id or (_verified(call) and r.get("phone") == call["caller"])]
    if not mine:
        _require_verified(call)
    return mine


@router.post("/internal/voice/calls")
async def start_call(body: StartIn) -> dict:
    line = await get_line(body.calledNumber)
    if not line:
        raise _err(404, "line_not_found")
    owner_id = line["ownerId"]
    cfg = await agent_config(owner_id)
    if _is_probe(cfg, body):
        # Our own forwarding check came back: no caller is on it, so no greeting is owed.
        await messaging_repo.modify_voice_agent(
            owner_id,
            lambda raw: {
                **{k: v for k, v in raw.items() if k != "forwardingCheck"},
                "forwardingOk": True,
                "forwardingOkAt": utc_now_iso(),
            },
            "voice",
        )
        await voice_outbound.end_probe(body.livekitRoom, cfg["forwardingCheck"]["callId"])
        raise _err(409, "forward_check")  # the worker leaves without speaking
    # The name the owner typed at setup is what callers know; the workspace name is only a fallback.
    business = (cfg.get("business") or "").strip()
    if not business:
        org = await clerk_admin.get_organization(owner_id) if owner_id.startswith("org_") else None
        business = (org or {}).get("name") or "the business"
    assistant = cfg["name"].strip() or "Lightbulb"
    if cfg.get("bookingsGo") == "google":  # a cold grant takes seconds; the in-call Google calls get 2 s
        _warming.add(t := asyncio.create_task(voice_booking.warm_google(owner_id)))
        t.add_done_callback(_warming.discard)
    caller = caller_id(body.callerNumber)
    verified = body.verstat.strip() in (
        "TN-Validation-Passed-A",
        "TN-Validation-Passed-A-Passthrough",
    )  # B, C, none, failed: the number may be spoofed
    seen = await _seen(owner_id, caller) if verified else None  # no name or memory by a spoofable number
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
        "callerVerified": verified,  # _verified(): what the caller's number may unlock on every later route
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
    await run_in_thread(  # the stale-call sweep's index: removed by end_call (voice_calls.sweep_phone_calls)
        messaging_table().put_item,
        Item={"pk": OPEN_CALLS_PK, "sk": call_id, "ownerId": owner_id, "startedAtEpoch": int(time.time())},
    )
    # Two overlapping starts for one LiveKit room each minted a token; only the stored one opens the call's routes,
    # so the answer is always the persisted META's, never this request's candidate.
    token = str(room.get("callToken") or "")
    if front:  # lever 2 (router-latency-2026-11-08.md): the Router's session is warm before the caller's first line
        rooms.prewake_owner(owner_id, team=[front])
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
        "instructions": owner_instructions(cfg),
        "callerName": call["callerName"] if allow["greetByName"] else "",
        "callerMemory": call["callerMemory"],
        "callerVerified": verified,  # the worker's prompt: unverified means no existing bookings, offer a message
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
        if await store.claim_voice_slot(owner_id, sk, call_id, LIVE_SLOT_S):
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
    if call.get("status") != "live":
        return {"ok": False}
    member = await phone_pin.check(ws, call.get("caller") or "", body.pin)
    if member:
        try:
            dm = await owner_dm(ws, member)
        except (messaging.ApiProblem, task_conversation.NoFrontAgent):  # a removed member, or no assistant yet
            member = None
    if not member:
        await _patch_call(call_id, lambda c: {"steps": _step(c, "A phone PIN was entered and refused")})
        return {"ok": False}
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
    if rooms._voice_requester(members, ws, call["ownerModeUserId"]):
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


#: A caller's opening that is not yet their request ("Hi.", "Hello, this is Sam.", "Yes.").
_NOT_A_REQUEST = re.compile(r"^(hi|hello|hey|good (morning|afternoon|evening)|yes|yeah|yep|no|ok|okay|sure|thanks|"
                            r"thank you|this is|my name is|it'?s)\b[\w ,']{0,24}[.!?]?$", re.I)  # fmt: skip


def call_summary(call: dict, lines: list[dict], worker: str = "") -> str:
    """One line for the Home row and the call's detail: what the caller wanted (their own first request, quoted)
    and what happened (the record's steps, which only the tools write, so nothing is claimed that was not done).
    10 Nov prod: rows read "Caller asked: Hi, this is Sam Tester. Can you cancel…" or "Open conversation"."""
    said = [s for x in lines if x["who"] == "caller" for s in re.split(r"(?<=[.?!])\s+", x["text"].strip()) if s]
    ask = next((s for s in said if "?" in s and not _NOT_A_REQUEST.match(s)), None) or next(
        (s for s in said if not _NOT_A_REQUEST.match(s)), ""
    )
    if len(ask) > 120:
        ask = ask[:120].rsplit(" ", 1)[0] + "…"
    done = "; ".join(s["text"] for s in call.get("steps") or []) or worker
    if not done:
        done = "Answered on the call." if ask else "Answered the call; no request made."
    elif done[-1] not in ".!?…":
        done += "."
    return (f'"{ask}" ' if ask else "") + done


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
            "summary": call_summary(call, lines, body.summary),
            "transcript": lines,
        }

    out = await _patch_call(call_id, change)
    if meta.get("liveSlot"):
        try:  # best-effort: the slot's lease expires on its own, and billing below must always run
            await store.release_voice_slot(meta["ownerId"], meta["liveSlot"], call_id)
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
    # An unattested call never writes a name, facts or a visit onto that number's row.
    seen = await _seen(meta["ownerId"], out["caller"]) if _verified(out) else None
    if _verified(out) and (seen or {}).get("lastCallId") != call_id:  # a retried end counts the visit once
        seen = seen or {}
        item = {**_caller_key(meta["ownerId"], out["caller"]), "lastCallAt": out["endedAt"], "lastCallId": call_id,
                "visits": int(seen.get("visits", 0)) + (seen.get("lastCallId") != call_id)}  # a retried end counts once  # fmt: skip
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
        await run_in_thread(messaging_table().put_item, Item=item)
    try:  # best-effort: the one outcome metric must never fail the route
        put_metric("VoiceCallOutcome", dimensions={"outcome": out["outcome"]})
    except Exception:
        logger.warning("VoiceCallOutcome metric failed", exc_info=True)
    if out["outcome"] not in NO_CALL_PUSH and meta.get("ownerUserId"):
        await _first_calls_push(meta["ownerUserId"], meta["ownerId"], call_id, out)
    asks = [("didnt_know", q) for q in dict.fromkeys(out.get("unknown", []))]
    asks += [  # every taken message is the owner's to call back: a card in Needs you, not only a Home row
        ("message", m["text"] + (f" (call back on {m['callbackNumber']})" if m.get("callbackNumber") else ""))
        for m in out.get("messages", [])
    ]
    if out.get("handoffMissed"):
        asks.append(("handoff", ""))  # nobody free or the transfer failed: line_problem is for system issues
    for kind, question in asks if meta.get("ownerUserId") else ():
        ask = call_ask(call_id, out, kind, question)
        await _start_task(call_id, meta, out, ask["title"], f"{ask['title']}\n{ask['text']}", ask=ask)
    await run_in_thread(messaging_table().delete_item, Key={"pk": OPEN_CALLS_PK, "sk": call_id})
    return out


# After Go live the owner hears about the first answered calls, so they see it working; then no more.
# Cards (needs_you, handoff_missed, message) push on their own, silent/forwarded calls were not answered.
NO_CALL_PUSH = {"silent", "forwarded", "needs_you", "handoff_missed", "message"}
CALL_PUSHES = 3


async def _first_calls_push(user_id: str, ws: str, call_id: str, call: dict) -> None:
    """One conditional write both admits a call to the first three and dedupes a retried end (the id is in the set)."""
    try:
        await run_in_thread(
            messaging_table().update_item,
            Key={"pk": owner_pk(ws), "sk": "CALLPUSH"},
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


MAX_FACTS = 10


def _to_add(cfg: dict, item: str) -> list[str]:
    return list(dict.fromkeys([*(cfg.get("toAdd") or []), item[:TO_ADD_CHARS]]))[-TO_ADD_MAX:]


# --- the assistant's own calendar (spec §4.1b: a hold in its calendar when the business has no API) --------


class DateIn(BaseModel):
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    service: str = Field(default="", max_length=200)


@router.post("/internal/voice/calls/{call_id}/availability")
async def availability(call_id: str, body: DateIn) -> dict:
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "book", "move")
    minutes, lanes = _minutes(cfg, body.service), _lanes(cfg, body.service)
    slots = await voice_booking.free(meta["ownerId"], cfg, call_id, body.date, minutes, lanes)
    return {"date": body.date, "slots": slots}


class BookIn(BaseModel):
    start: _Local
    service: _Str
    name: _Str


@router.post("/internal/voice/calls/{call_id}/book")
async def book(call_id: str, body: BookIn) -> dict:
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "book")
    ws = meta["ownerId"]
    result = await voice_booking.book(ws, cfg, call_id, call, body.start, body.service, body.name)
    if result["state"] != "confirmed":
        await _patch_call(call_id, lambda c: {"steps": _step(c, f"Booking not confirmed: {result['code']}")})
        return {"booked": False, **result}
    entry = {"start": body.start, "service": body.service, "name": body.name}
    who = f" with {result['staff']}" if result.get("staff") else ""
    line = f"{body.service} · {_pretty(body.start)} for {body.name}{who}"
    if not result.get("repeat"):  # a retry already has its line on the record
        await _patch_call(call_id, lambda c: {"bookings": [*c.get("bookings", []), entry], "booking": line,
                                          "callerName": c.get("callerName") or body.name,
                                          "steps": _step(c, f"Booked {line}")})  # fmt: skip
    return {"booked": True, **entry, **({"staff": result["staff"]} if result.get("staff") else {})}


class ChangeIn(BaseModel):
    start: _Local
    newStart: _Local | None = None  # None cancels
    bookingId: str = Field(default="", max_length=200)  # from my-bookings: which one, when two share a start


@router.post("/internal/voice/calls/{call_id}/change")
async def change_booking(call_id: str, body: ChangeIn) -> dict:
    """Move or cancel a booking -- only one made from this caller's number (a contact never touches another's)."""
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "move")
    # Every person's booking at that start (VOICEBOOK#<start>[#<id>]), only this caller's (_callers).
    # One caller can hold two at one start (two people): then `bookingId` says which, or the model is asked to.
    mine = _callers(await _bookings(meta["ownerId"], body.start), call_id, call)
    if body.bookingId:
        mine = [r for r in mine if body.bookingId == _booking_id(r)]
    if not mine:
        raise _err(404, "booking_not_found")
    if len(mine) > 1:
        options = [{"bookingId": _booking_id(r), "service": r["service"], "name": r["name"],
                    **({"staff": r["staff"]} if r.get("staff") else {})} for r in mine]  # fmt: skip
        raise HTTPException(status_code=409, detail={"code": "ambiguous_booking", "options": options})
    row = mine[0]
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


def _booking_id(row: dict) -> str:
    """A booking's id for the caller's tools; a row from before booking ids had none and is named by its key."""
    return row.get("bookingId") or row["sk"]


@router.post("/internal/voice/calls/{call_id}/my-bookings")
async def my_bookings(call_id: str) -> dict:
    meta, call = await _call(call_id)
    _allowed(await agent_config(meta["ownerId"]), call, "move")
    rows = _callers(await _bookings(meta["ownerId"], ""), call_id, call)

    def public_booking(b: dict) -> dict:
        out = {"start": _start(b), "service": b["service"], "bookingId": _booking_id(b)}
        if b.get("staff"):
            out["staff"] = b["staff"]
        if b.get("pending") or b.get("bookingState") == "held":
            out |= {
                "state": "pending" if b.get("pending") else "held",
                "confirmed": False,
                "pendingAction": (b.get("pending") or {}).get("action"),
                "requestedStart": (b.get("pending") or {}).get("newStart"),
            }
        return out

    return {"bookings": [public_booking(b) for b in rows]}


# --- waitlist (join half; texting a freed slot needs SMS registration and is not built) -------------------


class WaitlistIn(BaseModel):
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    service: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=300)  # the time they would take ("after 1pm"), in the caller's words


@router.post("/internal/voice/calls/{call_id}/waitlist")
async def join_waitlist(call_id: str, body: WaitlistIn) -> dict:
    """Put this call's caller on the waitlist for a day. Keyed to the call's caller number, never to the model.
    One entry per call per day (the key), so a retry rewrites it.
    ponytail: no per-caller daily cap; add one if a caller is seen filling the list."""
    meta, call = await _call(call_id)
    owner_id = meta["ownerId"]
    cfg = await agent_config(owner_id)
    _allowed(cfg, call, "waitlist")
    phone = _e164(call.get("caller") or "")
    if not phone:  # a withheld number cannot be called back, so it cannot wait
        raise _err(400, "caller_number_required")
    try:
        day = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise _err(422, "invalid_date") from None
    if day < _now(cfg).date():
        raise _err(400, "date_in_past")
    name = real_name(body.name) or call.get("callerName") or ""
    service = body.service.strip()
    row = {
        "pk": owner_pk(owner_id),
        "sk": f"VOICEWAIT#{body.date}#{call_id}",
        "id": call_id,
        "date": body.date,
        "phone": phone,
        "name": name,
        "service": service,
        "note": body.note.strip(),
        "callId": call_id,
        "createdAt": utc_now_iso(),
        "expiresAt": int(datetime.combine(day, datetime.min.time(), tzinfo=_tz(cfg)).timestamp()) + 2 * 86400,
    }  # TTL: a day after the date
    await run_in_thread(messaging_table().put_item, Item=row)
    line = f"Waitlisted for {body.date}" + (f" · {service}" if service else "")
    await _patch_call(call_id, lambda c: {"callerName": c.get("callerName") or name, "steps": _step(c, line)})
    return {"waitlisted": True, "date": body.date, "service": service}


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
                                          "steps": _step(c, f"Took a message: {body.text}")})  # fmt: skip
    return {"taken": True}


async def callers_own_words(call_id: str, text: str) -> bool:
    """A Front desk thread's message is the caller's: most of its words are words the caller said on this call (prod
    call: the thread filed its own answer, the open times, as a message nobody left, and the owner got a task for it).
    ponytail: content-word overlap with the caller's lines, as router_prompt.restates; an intent check if a real
    message that is all paraphrase gets refused (the thread is told to write it in the caller's words)."""
    from core.temporal.router_prompt import _STOP

    word = re.compile(r"[a-z0-9]+")  # digits count: a message that is a number or a time is still the caller's

    rows = await messaging_repo.list_messages_before(call_id, 10**19, 50)
    said = " ".join(r.get("text") or "" for r in rows if (r.get("author") or {}).get("id") == f"caller:{call_id}")
    words, heard = set(word.findall(text.lower())) - _STOP, set(word.findall(said.lower()))
    return len(words & heard) * 2 >= len(words)  # nothing to check ("call me") is not an invented answer


class UnknownIn(BaseModel):
    question: _Str


@router.post("/internal/voice/calls/{call_id}/unknown")
async def didnt_know(call_id: str, body: UnknownIn) -> dict:
    """A question the assistant couldn't answer: the call lands in Needs you."""
    meta, _ = await _call(call_id)
    body = UnknownIn(question=redact_cards(body.question))  # it also lands in the Agent page's toAdd
    await _patch_call(call_id, lambda c: {"unknown": [*c.get("unknown", []), body.question],
                                          "steps": _step(c, f"Didn't know: {body.question[:160]}")})  # fmt: skip
    await messaging_repo.modify_voice_agent(
        meta["ownerId"],
        lambda cfg: (
            None
            if body.question[:TO_ADD_CHARS] in (cfg.get("toAdd") or [])
            else {**cfg, "toAdd": _to_add(cfg, body.question)}
        ),
        meta.get("ownerUserId") or "",
    )
    return {"noted": True}


def handoff_to(cfg: dict) -> str | None:
    """The number a person-request may be handed to right now, or None: unset, or closed with onlyOpenHours."""
    h, now = cfg["handoff"], _now(cfg)
    window = _open_window(cfg, now.strftime("%Y-%m-%d"))
    is_open = bool(window) and window[0] <= now < window[1]
    to = us_number(str(h["number"] or ""))  # a number saved before the US/CA check is never dialed (M2)
    return to if to and (is_open or not h["onlyOpenHours"]) else None


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
    to = handoff_to(cfg)
    if (body and body.failed) or not to:
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
    await store.put_voice_run(
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


async def start_work(call_id: str, body: WorkIn) -> dict:
    """Open-ended work becomes a Worker task (the existing voice-start path), marked `origin: phone_contact` so
    effect_check asks the owner before any outside action. Called by voice_booking's line problem; no HTTP route
    (the worker never called `/start-work`). The same title in one call is the same task (a retried call)."""
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
    "the call: one or two short spoken sentences, no lists, links or markdown. Offer at most three open times. "
    "Never give the caller another phone number to call: when something cannot be done here, offer to take a "
    "message for the owner.)"
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
    who = f"Caller {call.get('callerName') or call.get('caller') or ''}".strip()
    line = await api.append_line(
        LineIn(
            tenant_id=ws,
            conversation_id=call_id,
            principal=Principal(kind="contact", id=f"caller:{call_id}", display_name=who),
            text=body.text,
            idempotency_key=f"call-turn:{call_id}:{body.n}",
            turn=body.n,
            via="voice",
            requester_user_id=requester,  # the line's owner user, as start_work's tasks: written by start_call
            trigger_key=f"turn-{body.n}",
            hint=CALLER_HINT,
            origin="phone_contact",
            product_ref={"callId": call_id, "from": who},
        )
    )
    return {"messageId": line.message_id, "triggerId": line.trigger_id,
            **({"taskId": line.task_id} if line.task_id else {}), **ended}  # fmt: skip


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
    if out["messages"]:
        await _caller_words(meta["ownerId"], out["messages"])
    claimed = [m for m in out["messages"] if BOOKING_NOW.search(_plain(m["text"]))]
    if (
        claimed
        and not call.get("bookings")
        and not await _has_upcoming(meta["ownerId"], call["caller"] if _verified(call) else "")
    ):
        for m in claimed:
            m["text"] = " ".join(
                NOT_BOOKED if BOOKING_NOW.search(_plain(s)) else s for s in re.split(r"(?<=[.?!])\s+", m["text"])
            )
    for m in out["messages"]:
        if _PHONE.search(m["text"]):  # the caller's own, or the hand-off number while it takes calls, may be said
            ok = {call.get("caller") or "", handoff_to(await agent_config(meta["ownerId"])) or ""}
            m["text"] = no_other_number(m["text"], ok)
    return out


#: What the caller hears instead of someone else's progress: the assistant, in the first person, holding.
HOLD = "One moment while I check."

#: A hold sentence that narrates someone else's work in progress ("They're still working on it", "The team is
#: checking this for you", "They're finalizing the cancellation", a bare "On it!"). Anchored on a third-party
#: subject, so "There's a $5 processing fee", "Maria is working on Saturdays" or "We're checking you in" never match.
THIRD_PARTY_PROGRESS = re.compile(
    r"^(?:on it\b|(?:they|he|she|someone|the team)\b[^.?!]*?(?:\b(?:is|are)|'re|'s)\s+(?:still\s+)?"
    r"(?:working on|checking|looking into|finali[sz]ing|handling|processing)\b)",
    re.I,
)


async def _caller_words(ws: str, messages: list[dict]) -> None:
    """The caller hears the business's assistant and outcomes, never the team behind it (10 Nov prod calls:
    "Customer Operations is checking this for you", "They're finalizing the cancellation"). Every line a caller
    hears comes through `/events`, so this is the one place.

    Every line: a specialist's name or title becomes "the team" ("the Finance team" too). A name followed by a
    capitalised word ("Hair Growth Treatment") or after "our"/"your"/"a" ("our Growth package") names a thing and is
    left alone, and so is the assistant's own name. A specialist the owner named after a real staff member is scrubbed
    as well ("Maria has an opening" -> "The team has an opening"): tolerable, never a leak.
    Hold lines only (`waiting`, an ack while a task runs): a sentence narrating a third party's progress becomes
    `HOLD`. An answer (`waiting` false) is never rewritten beyond the names, so its outcome always reaches the caller."""
    from core.services.workspace_provisioning import STARTER_ROSTER

    names = {r[1] for r in STARTER_ROSTER} | {r[3] for r in STARTER_ROSTER}
    try:
        names |= {n for a in await agent_registry_repo.list_agents(ws) for n in (a.get("name"), a.get("title")) if n}
        names -= {str((await agent_config(ws)).get("name") or "").strip()}
    except Exception:  # noqa: BLE001 -- unreadable: the starter names are still scrubbed, and the poll still answers
        logger.warning("phone: roster unreadable for %s; scrubbing starter names only", ws, exc_info=True)
    names.discard("")
    alts = "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
    pattern = re.compile(r"\b(?:(?P<det>(?i:the|our|your|my|an?))\s+)?(?:%s)(?P<team>\s+team)?\b" % alts)
    # ponytail: names re-read on every non-empty poll (1/s at most while lines arrive); cache if it ever shows

    def sub(text: str) -> str:
        def one(m: re.Match) -> str:
            det, before, after = m.group("det"), text[: m.start()], text[m.end() :]
            if re.match(r"\s[A-Z]", after):
                return m.group(0)  # part of a longer proper name
            if det and det.lower() != "the":
                return f"{det} team" if m.group("team") else m.group(0)  # "our Growth package" names a thing
            return "The team" if not before.strip() or before.rstrip()[-1] in ".?!" else "the team"

        return pattern.sub(one, text)

    for m in messages:
        m["text"] = sub(m["text"])
        if not m.get("waiting"):
            continue
        said: list[str] = []
        for s in re.split(r"(?<=[.?!])\s+(?=[A-Z])", m["text"].strip()):  # "3:30 p.m. tomorrow" stays whole
            s = HOLD if THIRD_PARTY_PROGRESS.match(_plain(s)) else s
            if not (s == HOLD and said and said[-1] == HOLD):
                said.append(s)
        m["text"] = " ".join(said)


#: A phone number in a reply ("484-616-3004", "(484) 616 3004", "+1 484.616.3004").
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SEND_ELSEWHERE = re.compile(r"\b(call|ring|dial|text|reach|contact)\b", re.I)
OTHER_NUMBER = "I can take a message for the owner instead."


def no_other_number(text: str, allowed: set[str]) -> str:
    """A sentence that sends the caller to another number is said as an offer to take a message (10 Nov prod: a
    refused cancel told the caller to ring the salon's listed number). `allowed`: numbers that may be said (the
    caller's own read back; the hand-off number while it takes calls)."""
    ok = {re.sub(r"\D", "", n)[-10:] for n in allowed if n}

    def elsewhere(sentence: str) -> bool:
        return bool(_SEND_ELSEWHERE.search(sentence)) and any(
            re.sub(r"\D", "", n)[-10:] not in ok for n in _PHONE.findall(sentence)
        )

    out = [OTHER_NUMBER if elsewhere(s) else s for s in re.split(r"(?<=[.?!])\s+", text)]
    return " ".join(dict.fromkeys(out)) if OTHER_NUMBER in out else text


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
    from core.voice.phone import business_profile, voice_booking

    now = f"{_now(await agent_config(ws)):%Y-%m-%dT%H:%M}"
    return any(
        b.get("phone") == caller and voice_booking.confirmed(b) for b in await business_profile._upcoming(ws, now)
    )


NOT_BOOKED = "That booking isn't made yet."
