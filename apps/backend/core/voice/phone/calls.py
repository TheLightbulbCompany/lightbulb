"""A phone call's record and the line's config, shared by the call routes (routers/voice_phone.py), the booking
engine (voice_booking), the Business profile and the questions a call leaves in Needs you: the Agent page with its
defaults, the call record (card-scrubbed, under its revision CAS), the call's Questions, and the bookings and open
slots the line takes."""

import functools
import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.billing import voice_billing
from core.config import settings
from core.dynamodb import get_item, is_cancelled, messaging_table, owner_pk, run_in_thread, transact, utc_now_iso
from core.engine import api
from core.engine.contract import AnnotateIn, ConversationOut, EngineError
from core.repositories import billing_repo
from core.voice import store
from core.voice.phone import voice_booking, voice_preferences
from core.voice.worker_api import _problem as _err

# The name these lines always logged under (routers/voice_phone.py): the W85 alarm matches loggers `routers.voice*`.
# Kept on the old router name: alarm W85 (observability-stack.ts) filters on loggers named routers.voice*.
logger = logging.getLogger("routers.voice_phone")


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
    "allow": {"book": True, "move": True, "waitlist": True, "greetByName": True, "answer": True},
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


async def agent_config(owner_id: str) -> dict:
    cfg = {**DEFAULTS, **(await store.get_voice_agent(owner_id) or {})}
    for k in ("allow", "hours", "handoff"):
        cfg[k] = {**DEFAULTS[k], **(cfg.get(k) or {})}
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    cfg["_paymentBlocked"] = bool(
        account.get("metronome_voice_contract_id") and not voice_billing.has_voice_plan(account)
    )
    return cfg


def owner_instructions(cfg: dict) -> str:
    """The Agent page's instructions, the one text every phone model reads: the voice's prompt (start_call) and the
    Router's and Front desk thread's Business profile (business_profile)."""
    return str(cfg.get("instructions") or "").strip()[:4000]


async def _call(owner_id: str, call_id: str) -> tuple[ConversationOut, dict]:
    """The owner's call conversation and its record, or 404."""
    conv = await api.get_conversation(owner_id, call_id)
    if not conv or conv.kind != "call" or not isinstance(conv.annotations.get("call"), dict):
        raise _err(404, "call_not_found")
    return conv, conv.annotations["call"]


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


async def _patch_call(owner_id: str, call_id: str, change) -> dict:
    """Read-modify-write of META.call under the revision CAS (one worker per call, so a race is rare). Every write
    of the record (steps, transcript, messages, summary) is card-scrubbed and keeps only a real caller name."""
    for attempt in (1, 2, 3):
        conv, call = await _call(owner_id, call_id)
        merged = _scrub({**call, **change(call)})
        merged["callerName"] = real_name(merged.get("callerName"))
        try:
            await api.annotate(
                AnnotateIn(
                    tenant_id=owner_id, conversation_id=call_id, set={"call": merged}, expected_revision=conv.revision
                )
            )
            return merged
        except ClientError:
            if attempt == 3:
                raise
        except EngineError as exc:
            if exc.status != 404:
                raise
            raise _err(404, "call_not_found") from None  # the call's room is gone, as `_call` answers
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


def _verified(call: dict) -> bool:
    """The carrier attested at level A that the caller owns their number (founder 9 Nov, security review). Only then
    does the number grant anything: name, caller memory, existing bookings, move and cancel. Anyone can still book
    and leave a message. A record written before this field reads as unverified."""
    return bool(call.get("caller")) and call.get("callerVerified") is True


def _facts(cfg: dict) -> str:
    """What the assistant may tell callers (allow.answer): city, services with prices and durations, hours, closures,
    staff and the owner's answers. The phone voice gets this whole at call start and states it itself; the Router's
    Business profile and a Worker's business_facts read the same text."""
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
    ranges = cfg.get("holidays") if isinstance(cfg.get("holidays"), list) else []  # an old free-text value: none
    closed = ", ".join(
        f"{r['from']} to {r.get('to') or r['from']}" for r in ranges if isinstance(r, dict) and r.get("from")
    )
    staff = ", ".join(n for _, n in _staff(cfg))
    city = str(cfg.get("city") or "").strip()
    return (
        (f"City: {city}. " if city else "")
        + f"Services: {services or 'not listed'}. Opening hours: {hours}. "
        + (f"Closed: {closed}. " if closed else "")
        + (f"Staff: {staff}. " if staff else "")
        + qa
    ).strip()


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


OPEN_CALLS_PK = "VOICEOPENCALLS"


CALL_OPTIONS = {
    "didnt_know": [("answer", "Answer"), ("call_myself", "I'll call them"), ("done", "Done")],
    "handoff": [("call_myself", "I'll call them"), ("done", "Done")],
    "line_problem": [("call_myself", "I'll call them"), ("done", "Done")],
    "message": [("call_myself", "I'll call them"), ("done", "Done")],
}


KNOWLEDGE_SOURCE = "you answered this in Needs you"


def call_ask(call_id: str, call: dict, kind: str, question: str = "") -> dict:
    """`task_asks.open_ask`'s arguments for one call Question (a blocking `question`, subkind = the card's kind),
    but its qid: the task it opens on names it (`api.start_task`), so a retried end opens it once."""
    who = call.get("callerName") or call.get("caller") or "a caller"
    title = {"didnt_know": f"Answer {who}: {question}", "handoff": f"{who} wanted a person",
             "line_problem": f"{who}'s call hit a problem on the line",
             # the message is in the title: the title is the call's task id (_start_task), one per message
             "message": f"Message from {who}: {question}"}[kind]  # fmt: skip
    return {
        "kind": "question",
        "subkind": kind,
        "title": title[:200],
        "text": (f"{question}\n" if kind == "message" else "") + f"Caller: {call.get('caller') or 'unknown number'}",
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
    save). Idempotent. The write is conditional on the document it read, so it never undoes an owner's save."""
    item = {"q": question, "a": answer, "source": KNOWLEDGE_SOURCE}

    def change(cfg: dict) -> dict | None:  # no Agent page yet: modify_voice_agent writes nothing (skips onboarding)
        knowledge, to_add = list(cfg.get("knowledge") or []), list(cfg.get("toAdd") or [])
        if item in knowledge and question[:TO_ADD_CHARS] not in to_add:
            return None
        return {
            **cfg,
            "knowledge": knowledge if item in knowledge else [*knowledge, item],
            "toAdd": [t for t in to_add if t != question[:TO_ADD_CHARS]],
        }

    await store.modify_voice_agent(owner_id, change, by)


TO_ADD_MAX, TO_ADD_CHARS = 20, 200  # the Agent page's "questions to add": caller text, so capped (oldest drop)


async def resolve_call(owner_id: str, call_id: str, by: str) -> None:
    """A call's Question was answered: the call is handled (`resolvedAt`), so Home's
    "Needs you" stops listing it. ponytail: the first answered Question resolves a call that had several."""
    conv = await api.get_conversation(owner_id, call_id)
    if not conv or conv.kind != "call":
        return
    await _patch_call(
        owner_id, call_id, lambda c: {} if c.get("resolvedAt") else {"resolvedAt": utc_now_iso(), "resolvedBy": by}
    )


def _service(cfg: dict, service: str) -> dict:
    s = service.strip().lower()
    for x in cfg["services"]:
        n = str(x.get("name", "")).strip().lower()
        if n and (n in s or s in n):
            return x
    return {}


def _minutes(cfg: dict, service: str) -> int:
    return int(_service(cfg, service).get("minutes") or 60)


def _staff(cfg: dict) -> list[tuple[str, str]]:
    """The Agent page's people as (id, name), at most 20. The id is assigned when the person is added and kept on
    rename, so a person's bookings never depend on their name or place in the list. A bare string is its own id."""
    out: dict[str, str] = {}
    for p in cfg.get("staff") if isinstance(cfg.get("staff"), list) else []:
        pid, name = (p, p) if isinstance(p, str) else (p.get("id"), p.get("name")) if isinstance(p, dict) else ("", "")
        if isinstance(pid, str) and isinstance(name, str) and pid.strip() and name.strip():
            out.setdefault(pid.strip()[:40], name.strip()[:40])
    return list(out.items())[:20]


def _lanes(cfg: dict, service: str) -> list[tuple[str, str]]:
    """(key suffix, name) for everyone who can do `service`, in list order; "" (no service) is the whole roster.
    A person's keys carry their id (VOICEBOOK#<start>#<id>), so one person is never booked twice at once. The
    unsuffixed keys mean only "no staff list": a config without one has the single lane "" (today's keys). How many
    bookings a slot takes, across every lane, is `_capacity`'s. A service's `staff` ("a1, m1") lists who does it by
    id (a name also matches); empty, or naming nobody on the list, is anyone. Per-person schedules and days off are
    not modelled."""
    staff = _staff(cfg)
    if not staff:
        return [("", "")]
    svc = _service(cfg, service) if service.strip() else {}  # "" would match the first service
    named = {n.strip().lower() for n in str(svc.get("staff") or "").split(",")}
    eligible = [(i, n) for i, n in staff if i.lower() in named or n.lower() in named] or staff
    return [(f"#{i}", n) for i, n in eligible]


def _start(row: dict) -> str:
    """A VOICEBOOK# row's local start (the 16 characters after the prefix; a person's suffix may follow)."""
    return row["sk"][10:26]


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


def _capacity(cfg: dict) -> int:
    """Bookings one slot can hold, across every lane: one per person on the roster, one with no roster. Bookings
    that go to Google share the business's one calendar, which cannot hold two at once: always one there, so a
    held booking (its Google write still pending) blocks the slot for everyone."""
    return 1 if cfg.get("bookingsGo") == "google" else max(1, len(_staff(cfg)))


def open_starts(
    cfg: dict, date: str, taken: list[tuple], now: datetime, minutes: int, lanes: list[tuple[str, str]]
) -> list[str]:
    """`free_slots` where one of `lanes` (the people who can do it) is free and the slot, counting every booking in
    every lane (old unsuffixed ones and people no longer on the roster too), is below `_capacity`: the rule
    `_capacity_items` enforces when booking. `taken` holds (start, end, lane) bookings and (start, end) blocks, such
    as a Google busy block, that every lane shares."""
    shared = [(t[0], t[1]) for t in taken if len(t) == 2]
    every = [(t[0], t[1]) for t in taken if len(t) == 3]
    need, buf, cap = timedelta(minutes=minutes), timedelta(minutes=int(cfg.get("bufferMin") or 0)), _capacity(cfg)
    out: set[str] = set()
    for lane, _ in lanes:
        own = shared + [(t[0], t[1]) for t in taken if len(t) == 3 and t[2] == lane]
        for at in free_slots(cfg, date, own, now, minutes):
            t = datetime.strptime(at, "%Y-%m-%dT%H:%M")
            if _peak(t, need + buf, buf, every) < cap:
                out.add(at)
    return sorted(out)


def _peak(at: datetime, span: timedelta, buf: timedelta, taken: list[tuple[datetime, datetime]]) -> int:
    """Most bookings (each held with its buffer, as its cells are) at any 5 minutes of [at, at + span)."""
    steps = range(int(span / timedelta(minutes=5)) or 1)
    return max(sum(s <= at + timedelta(minutes=5 * i) < e + buf for s, e in taken) for i in steps)


def _clear(at: datetime, need: timedelta, buf: timedelta, taken: list[tuple[datetime, datetime]]) -> bool:
    return all(at + need + buf <= s or e + buf <= at for s, e in taken)


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
        if at > now and _clear(at, need, buf, taken):
            out.append(at.strftime("%Y-%m-%dT%H:%M"))
        at += _GRID
    return out


async def _booking_rows(owner_id: str, prefix: str) -> list[dict]:
    args = {
        "KeyConditionExpression": Key("pk").eq(owner_pk(owner_id)) & Key("sk").begins_with(prefix),
        "ConsistentRead": True,
    }
    rows = []
    while True:
        result = await run_in_thread(messaging_table().query, **args)
        rows.extend(result.get("Items", []))
        if not result.get("LastEvaluatedKey"):
            return rows
        args["ExclusiveStartKey"] = result["LastEvaluatedKey"]


async def _bookings(owner_id: str, prefix: str) -> list[dict]:
    return await _booking_rows(owner_id, f"VOICEBOOK#{prefix}")


async def _taken(owner_id: str, date: str) -> list[tuple[datetime, datetime, str]]:
    """(start, end, lane) for every booking on `date`. Provisional move targets stay visible, including moves to a
    different date; a hold's lane is its `lane` (a hold written before lanes had none: the one lane)."""
    holds = await _booking_rows(owner_id, f"VOICEHOLD#{date}")
    out = []
    for b in [*(await _bookings(owner_id, date)), *holds]:
        hold = b["sk"].startswith("VOICEHOLD#")
        s = datetime.strptime(b["start"] if hold else _start(b), "%Y-%m-%dT%H:%M")
        out.append((s, s + timedelta(minutes=int(b.get("minutes") or 60)), b.get("lane", "") if hold else b["sk"][26:]))
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


def _cells(cfg: dict, start: str, minutes: int, lane: str = "") -> list[str]:
    """The 5-minute UTC cells a booking holds in its lane: [start, start + minutes + bufferMin)."""
    t = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=_tz(cfg)).astimezone(ZoneInfo("UTC"))
    n = -(-(minutes + int(cfg.get("bufferMin") or 0)) // 5)
    return [f"VOICECELL#{(t + timedelta(minutes=5 * i)):%Y-%m-%dT%H:%M}{lane}" for i in range(n)]


async def _book(owner_id: str, call_id: str, call: dict, cfg: dict, start: str, service: str, name: str,
                old: dict | None = None) -> dict:  # fmt: skip
    """Write the booking and its cells in one TransactWriteItems with `attribute_not_exists` on every cell, so an
    overlap fails atomically (https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html,
    https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html). `old` is the booking
    being moved: its row and cells go in the same transaction. Returns the booking row written."""
    minutes, pk, table = (
        _minutes(cfg, service),
        owner_pk(owner_id),
        settings.MESSAGING_TABLE,
    )
    if start not in free_slots(cfg, start[:10], [], _now(cfg), minutes) or (old and _start(old) == start):
        raise _err(400, "slot_unavailable")
    # A move keeps its person; a new booking takes the first eligible person whose keys are free. The slot's
    # capacity (every lane counted) is checked once and guarded in the same transaction. A lost race on one
    # person's keys (the transaction is cancelled) tries the next person.
    lanes = [(old["sk"][26:], old.get("staff", ""))] if old else _lanes(cfg, service)
    mine = set(old.get("cells", [])) if old else set()
    # Another booking landing between the read and the write changes the guards' versions and cancels every lane's
    # write with them: re-read the slot (which refuses 409 once it is truly full) and try the people again.
    # A new booking also writes an idempotency row for (call, start, service, name), whoever it takes: two identical
    # requests at once cannot both pass it, and the second returns the first's booking (marked `_repeat`).
    key = (
        None
        if old
        else f"VOICEBOOKKEY#{hashlib.sha256(f'{call_id}|{start}|{service}|{name}'.encode()).hexdigest()[:40]}"
    )
    for _ in range(3):
        guards = await _capacity_items(owner_id, cfg, _cells(cfg, start, minutes), mine)
        for lane, person in lanes:
            items = _booking_items(table, pk, call_id, call, cfg, start, service, name, minutes, lane, person, old)
            if key:
                row = items[0]["Put"]["Item"]
                items.append({"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(sk)", "Item": {
                    "pk": pk, "sk": key, "booking": row["sk"], "bookingId": row["bookingId"],
                    "expiresAt": _cell_expiry(row["cells"][-1])}}})  # fmt: skip
            if len(items) + len(guards) > 100:
                raise _err(400, "booking_too_long")
            try:
                await transact(messaging_table(), items + guards)
                return items[0]["Put"]["Item"]
            except ClientError as exc:
                if not is_cancelled(exc):
                    raise
                if key and (same := await _same_booking(owner_id, key)):
                    return same
    raise _err(409, "slot_taken")


async def _same_booking(owner_id: str, key: str) -> dict | None:
    """The booking an identical request already made, or None. A key whose booking is gone (cancelled or moved)
    is removed, so the same booking can be made again."""
    pk = owner_pk(owner_id)
    hit = await get_item(messaging_table(), pk, key)
    if not hit:
        return None
    row = await get_item(messaging_table(), pk, hit["booking"])
    if row and row.get("bookingId") == hit["bookingId"]:
        return {**row, "_repeat": True}
    try:
        await run_in_thread(messaging_table().delete_item, Key={"pk": pk, "sk": key},
                            ConditionExpression="bookingId = :bid", ExpressionAttributeValues={":bid": hit["bookingId"]})  # fmt: skip
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return None


async def _query_range(owner_id: str, first: str, last: str) -> list[dict]:
    args = {
        "KeyConditionExpression": Key("pk").eq(owner_pk(owner_id)) & Key("sk").between(first, last + "\uffff"),
        "ConsistentRead": True,
    }
    out: list[dict] = []
    while True:
        result = await run_in_thread(messaging_table().query, **args)
        out.extend(result.get("Items", []))
        if not result.get("LastEvaluatedKey"):
            return out
        args["ExclusiveStartKey"] = result["LastEvaluatedKey"]


async def _capacity_items(owner_id: str, cfg: dict, adding: list[str], mine: set[str] = frozenset()) -> list[dict]:
    """The capacity rule for a write that adds booking cells at `adding`'s times: at each of those 5 minutes, the
    cells already there in ANY lane (unsuffixed, every person, people since removed), less `mine` (the booking
    being moved, released in the same transaction), must be below `_capacity`; else 409 slot_taken. Returns one
    guard item per time, `VOICECAP#<utc>`, whose version the transaction bumps on the condition that it is still the
    one read. Every write that ADDS cells bumps the guards of their times, so two of them over one slot serialise
    and the second, having counted without the first's cells, is cancelled. Releases only lower the count, so they
    never touch a guard: a count that still includes a cell released since is merely cautious. The guards are read
    BEFORE the cells, so a write that lands between the reads is either counted or changes the version.
    ponytail: two items per cell, so the longest booking is about half the 99 cells it was (~4 h); a reservation
    that needs more is refused as booking_too_long."""
    if not adding:
        return []
    times = sorted({c[10:26] for c in adding})
    seen = {
        g["sk"].removeprefix("VOICECAP#"): g
        for g in await _query_range(owner_id, f"VOICECAP#{times[0]}", f"VOICECAP#{times[-1]}")
    }
    cells = await _query_range(owner_id, f"VOICECELL#{times[0]}", f"VOICECELL#{times[-1]}")
    count: dict[str, int] = {}
    for c in cells:
        if c["sk"] not in mine:
            count[c["sk"][10:26]] = count.get(c["sk"][10:26], 0) + 1
    if any(count.get(t, 0) >= _capacity(cfg) for t in times):
        raise _err(409, "slot_taken")
    table, pk = settings.MESSAGING_TABLE, owner_pk(owner_id)
    out = []
    for t in times:
        v = int(seen[t]["v"]) if t in seen else 0
        cond = {"ConditionExpression": "v = :v", "ExpressionAttributeValues": {":v": v}} if t in seen else {
            "ConditionExpression": "attribute_not_exists(sk)"}  # fmt: skip
        put = {"pk": pk, "sk": f"VOICECAP#{t}", "v": v + 1, "expiresAt": _cell_expiry(f"VOICECELL#{t}")}
        out.append({"Put": {"TableName": table, "Item": put, **cond}})
    return out


def _booking_items(table: str, pk: str, call_id: str, call: dict, cfg: dict, start: str, service: str, name: str,
                   minutes: int, lane: str, person: str, old: dict | None) -> list[dict]:  # fmt: skip
    cells = _cells(cfg, start, minutes, lane)
    if len(cells) > 99:  # DynamoDB caps a transaction at 100 items (the booking row is one)
        raise _err(400, "booking_too_long")
    sk, mine = f"VOICEBOOK#{start}{lane}", set(old.get("cells", [])) if old else set()
    items = [{"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(sk)", "Item": {
        "pk": pk, "sk": sk, "bookingId": (old or {}).get("bookingId") or str(uuid.uuid4()),
        "generation": int((old or {}).get("generation", 0)) + 1,
        "bookingProvider": (old or {}).get("bookingProvider") or ("google" if cfg.get("bookingsGo") == "google" else "own"),
        "bookingState": "held" if cfg.get("bookingsGo") == "google" else "confirmed",
        "service": service, "minutes": minutes, "name": name, "phone": call.get("caller", ""),
        "callId": call_id, "createdAt": utc_now_iso(), "cells": cells, **({"staff": person} if person else {})}}}]  # fmt: skip
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
    return items


def _cell_expiry(cell: str) -> int:  # the table's TTL attribute: the cell's end + 1 day
    t = datetime.strptime(cell[10:26], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("UTC"))  # after it: a lane
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


async def after_call(owner_id: str, room_id: str, item: dict) -> None:
    """A team answer that lands in a call's room after the caller hung up (8 Nov prod re-run #3: the booking
    confirmation and the card answer, 8-10 s after hang-up): kept on the call record (`afterCall`, and a step the
    Home row shows) so the owner sees what the caller never heard. Thread and progress lines are not answers."""
    if item.get("progress") or item.get("threadRootMessageId") or not (text := (item.get("text") or "").strip()):
        return
    conv = await api.get_conversation(owner_id, room_id)
    if not conv or conv.kind != "call" or (conv.annotations.get("call") or {}).get("status") != "ended":
        return
    await _patch_call(
        owner_id,
        room_id,
        lambda c: {
            "afterCall": [*c.get("afterCall", []), {"messageId": item.get("messageId"), "text": text[:2000]}],
            "steps": _step(c, f"After the call, not heard by the caller: {text[:160]}"),
        },
    )
