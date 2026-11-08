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
"""

import asyncio
import functools
import hashlib
import re
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from core.dynamodb import run_in_thread, utc_now_iso
from core.billing import voice_billing
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core import tasks_schema as ts
from core.services import (
    admission,
    clerk_admin,
    questions,
    task_conversation,
    voice_booking,
    voice_calls,
    voice_outbound,
)
from routers.huddles import _Worker
from routers.voice import _e164, month_seconds
from routers.voice_meet import agent_lines, latest_seq
from routers.internal_automations import VoiceStartIn, start_voice_work

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[_Worker])

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
    "live": False,
    "paused": False,
    "instructions": "",
    "allow": {"book": True, "move": True, "waitlist": True, "greetByName": True, "answer": True},
    "knowledge": [],
    "hours": {d: {"open": "09:00", "close": "18:00", "closed": d == "Sun"} for d in _DAYS},
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
    return cfg


def _line_state(cfg: dict, caller: str) -> Literal["live", "message_only"]:
    """Live after Go live and not paused; the owner's own number always gets the live line (the test call)."""
    owner = {_e164(str(n or "")) for n in (cfg["handoff"]["number"], cfg.get("forwardFrom"))} - {None}
    return "live" if (cfg.get("live") and not cfg.get("paused")) or _e164(caller) in owner else "message_only"


def _allowed(cfg: dict, call: dict, *caps: str) -> None:
    """Refuse (403) a capability the owner turned off, or everything but a message while the line is message-only."""
    if _line_state(cfg, call.get("caller", "")) != "live" or not any(cfg["allow"].get(c) for c in caps):
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
        # Our own forwarding check came back: no caller is on it, so no greeting (and no disclosure) is owed.
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
    seen = await _seen(owner_id, body.callerNumber)
    # Minutes used up and the owner chose "forward": the caller goes to the hand-off number instead of the assistant.
    # Never to the line that forwards here (the kept business number) or the one called: that rings us again.
    forward_to = _e164(str(cfg["handoff"]["number"] or "")) if cfg.get("runOut") == "forward" else None
    if forward_to in {_e164(str(cfg.get("forwardFrom") or "")), _e164(body.calledNumber)}:
        forward_to = None
    if forward_to:
        try:
            if await month_seconds(owner_id, cfg.get("timezone")) < voice_billing.INCLUDED_MINUTES * 60:
                forward_to = None
        except Exception:  # metering never drops a live call: answer as normal
            logger.warning("run-out check failed for %s; answering", owner_id, exc_info=True)
            forward_to = None
    call_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-call:{body.livekitRoom}"))
    call = {
        "caller": body.callerNumber,
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
    }
    name = f"Call · {call['callerName'] or body.callerNumber or 'unknown'}"[:60]
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
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=call_id,
            client_room_id=f"call:{body.livekitRoom}",
            creator_user_id=line["ownerUserId"],
            name=name,
            kind="call",
            members=members,
            req_hash=messaging_repo.request_hash(name, "call", body.livekitRoom),
            extra={"call": call, "hidden": True, "ownerUserId": line["ownerUserId"]},
        )
    except messaging_repo.MessagingRepoError:
        if await messaging_repo.get_room(call_id) is None:  # a replay keeps the room it made
            raise
    if front:  # lever 2 (router-latency-2026-11-08.md): the Router's session is warm before the caller's first line
        voice_calls.prewake_owner(owner_id, team=[front])
    allow, mode = cfg["allow"], _line_state(cfg, body.callerNumber)
    paused = mode != "live"
    return {
        "after": await latest_seq(call_id),  # the first `/events` cursor: a replayed start says nothing twice
        "mode": mode,
        "callId": call_id,
        "ownerId": owner_id,
        "businessName": business,
        "assistantName": assistant,
        "greeting": greeting(cfg["greeting"], business, assistant),
        "facts": _facts(cfg) if allow["answer"] and not paused else "",
        "instructions": cfg["instructions"][:4000],
        "callerName": call["callerName"] if allow["greetByName"] else "",
        "callerMemory": call["callerMemory"],
        "can": {k: bool(allow[k]) and not paused for k in ("book", "move", "answer")},
        "transfer": bool(cfg["handoff"]["number"]),
        "forwardTo": forward_to,  # the worker transfers the caller there, then ends with forwarded: true
        "today": datetime.now(_tz(cfg)).strftime("%A %Y-%m-%d %H:%M"),
    }


def _is_probe(cfg: dict, body: StartIn) -> bool:
    """A call arriving within WINDOW_S of a pending forwarding check, from our own line or the business line (the
    carrier may present either as the caller)."""
    if body.callerNumber not in (body.calledNumber, _e164(str(cfg.get("forwardFrom") or ""))):
        return False
    return 0 <= voice_outbound.age_s(cfg.get("forwardingCheck")) <= voice_outbound.WINDOW_S


def greeting(custom: str, business: str, assistant: str) -> str:
    """The owner's greeting only when it still discloses the AI, the recording and the assistant's name; else ours."""
    g = custom.strip()
    if all(w.lower() in g.lower() for w in ("AI assistant", "recorded", assistant)):
        return g
    return f"Hi, I'm {assistant}, {business}'s AI assistant, and this call is recorded. How can I help?"


class EndIn(BaseModel):
    durationSec: int = Field(ge=0, le=86400)
    summary: str = Field(default="", max_length=4000)
    transcript: list[dict] = Field(default=[], max_length=2000)
    #: Whether the transcribed first line carried the AI and recording disclosure (worker `disclosure_of`).
    disclosure: Literal["ok", "missing", "unknown"] | None = None
    disclosureRepeated: bool = False  # the worker said the disclosure sentence after a failed first line
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
async def end_call(call_id: str, body: EndIn, background: BackgroundTasks) -> dict:
    lines = transcript_lines(body.transcript)
    meta, _ = await _call(call_id)
    forwarded_to = (await agent_config(meta["ownerId"]))["handoff"]["number"] if body.forwarded else None
    before: dict = {}

    def change(call: dict) -> dict:
        before["status"] = call.get("status")
        if forwarded_to:
            return {"status": "ended", "endedAt": utc_now_iso(), "durationSec": body.durationSec,
                    "outcome": "forwarded", "transferredTo": forwarded_to,
                    "steps": _step(call, "Minutes used up: forwarded to you")}  # fmt: skip
        return {"status": "ended", "endedAt": utc_now_iso(), "durationSec": body.durationSec,
                "outcome": "silent" if body.silent and outcome_of(call) == "answered" else outcome_of(call),
                "summary": body.summary, "transcript": lines,
                **({"disclosure": body.disclosure, "disclosureRepeated": body.disclosureRepeated}
                   if body.disclosure else {})}  # fmt: skip

    out = await _patch_call(call_id, change)
    if before.get("status") != "ended":  # a retried end counts nothing twice
        try:  # the record is written: a counter failure must not 500 away caller memory, asks and the outcome
            await voice_billing.add_month_seconds(
                meta["ownerId"], body.durationSec, datetime.fromisoformat(out["startedAt"])
            )
        except Exception:
            logger.warning("voice minutes counter failed for %s", call_id, exc_info=True)
        background.add_task(
            voice_billing.report_minutes, meta["ownerId"], call_id, out["startedAt"], body.durationSec, "phone"
        )
    if out.get("caller"):
        seen = await _seen(meta["ownerId"], out["caller"]) or {}
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
    asks = [("didnt_know", q) for q in dict.fromkeys(out.get("unknown", []))]
    if out.get("handoffMissed"):
        asks.append(("handoff", ""))  # nobody free or the transfer failed: line_problem is for system issues
    for kind, question in asks if meta.get("ownerUserId") else ():
        ask = call_ask(call_id, out, kind, question)
        await _start_task(call_id, meta, out, ask["title"], f"{ask['title']}\n{ask['text']}", ask=ask)
    return out


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
        "preview": {"kind": kind, "roomId": call_id, "caller": call.get("caller", ""), "question": question},
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
    if _line_state(await agent_config(meta["ownerId"]), call.get("caller", "")) != "live":
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


def _open_window(cfg: dict, date: str) -> tuple[datetime, datetime] | None:
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


async def _bookings(owner_id: str, prefix: str) -> list[dict]:
    r = await run_in_thread(
        messaging_repo._table().query,
        KeyConditionExpression=Key("pk").eq(messaging_repo.owner_pk(owner_id))
        & Key("sk").begins_with(f"VOICEBOOK#{prefix}"),
    )
    return r.get("Items", [])


async def _taken(owner_id: str, date: str) -> list[tuple[datetime, datetime]]:
    out = []
    for b in await _bookings(owner_id, date):
        s = datetime.strptime(b["sk"].removeprefix("VOICEBOOK#"), "%Y-%m-%dT%H:%M")
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
        "pk": pk, "sk": sk, "service": service, "minutes": minutes, "name": name, "phone": call.get("caller", ""),
        "callId": call_id, "createdAt": utc_now_iso(), "cells": cells}}}]  # fmt: skip
    for c in cells:
        put = {
            "TableName": table,
            "Item": {"pk": pk, "sk": c, "booking": sk, "expiresAt": _cell_expiry(c)},
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
    return [
        {"Delete": {"TableName": table, "Key": {"pk": pk, "sk": k}}}
        for k in [row["sk"], *row.get("cells", [])]
        if k not in keep
    ]


@router.post("/internal/voice/calls/{call_id}/book")
async def book(call_id: str, body: BookIn) -> dict:
    meta, call = await _call(call_id)
    cfg = await agent_config(meta["ownerId"])
    _allowed(cfg, call, "book")
    await voice_booking.book(meta["ownerId"], cfg, call_id, call, body.start, body.service, body.name)
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
        await voice_booking.move(meta["ownerId"], cfg, call_id, call, row, body.newStart)
    else:
        await voice_booking.cancel(meta["ownerId"], cfg, call_id, row)
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
    return {"bookings": [{"start": b["sk"].removeprefix("VOICEBOOK#"), "service": b["service"]} for b in rows]}


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
    if _line_state(cfg, call.get("caller", "")) != "live":
        raise _err(403, "not_allowed")
    h, now = cfg["handoff"], _now(cfg)
    window = _open_window(cfg, now.strftime("%Y-%m-%d"))
    is_open = bool(window) and window[0] <= now < window[1]
    if (body and body.failed) or not h["number"] or (h["onlyOpenHours"] and not is_open):
        why = "the transfer didn't connect" if body and body.failed else "nobody was available"
        await _patch_call(call_id, lambda c: {"handoffMissed": True, "steps": _step(c, f"Asked for a person; {why}")})
        return {"transferTo": None}
    await _patch_call(call_id, lambda c: {"transferredTo": h["number"], "steps": _step(c, "Transferred to a person")})
    return {"transferTo": h["number"]}


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
    if _line_state(await agent_config(meta["ownerId"]), call.get("caller", "")) != "live":
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


@router.post("/internal/voice/calls/{call_id}/turn")
async def caller_turn(call_id: str, body: TurnIn) -> dict:
    """One delegated caller line: a message in the call's room, authored by the caller (a non-member: no admission
    row, no memory), and a `phone_contact` trigger for that room, so the Router decides it with the caller's scope
    and the Business profile, and its Front desk threads run as the caller. Keyed on (call, n): a retry is the same."""
    meta, call = await _call(call_id)
    body = TurnIn(n=body.n, text=redact_cards(body.text))  # the room message and the Router's trigger alike
    ws, requester = meta["ownerId"], meta.get("ownerUserId")
    if not requester:
        raise _err(403, "no_requester")
    if _line_state(await agent_config(ws), call.get("caller", "")) != "live":
        raise _err(403, "not_allowed")  # a paused line only takes messages (/message)
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
    await admission.enqueue_trigger(ws, trigger, room_id=call_id)
    return {"messageId": msg["messageId"], "triggerId": trigger.trigger_id}


class CallEventsIn(BaseModel):
    after: int = Field(ge=0)


@router.post("/internal/voice/calls/{call_id}/events")
async def call_events(call_id: str, body: CallEventsIn) -> dict:
    """Lightbulb's messages in the call's room after seq `after` (the Router's ack and replies, the Front desk's
    answers), oldest first: the phone voice says each once (huddle-agent mouth.py)."""
    await _call(call_id)
    return await agent_lines(call_id, body.after)
