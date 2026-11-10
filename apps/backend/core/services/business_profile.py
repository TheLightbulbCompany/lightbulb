"""The business's own facts for the phone front door (router-latency-2026-11-08.md: 0 of 17 caller questions right,
because only the phone code could read them). A caller's Router batch and its Front desk thread get this block;
owner and member chat, the orb and Meet never do (founder, 8 Nov).

It reuses the phone's readers: the Agent page (`voice_phone.agent_config`, `_facts`: services, prices, durations,
hours, the Knowledge answers that hold the policies), open slots (`voice_phone.free_slots`) and the caller's own
bookings (`VOICEBOOK#` rows with their number). Best-effort: bounded to ~2k characters, cached 60 s, 2 s at most.

Business facts and optional availability return independently: provider failures or
calendar-level errors mark availability unknown and never manufacture free slots.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TIMEOUT_S = 2.0
TTL_S = 60.0
GOOGLE_S = 1.0  # the week's freeBusy; past it external availability is unknown
ORG_S, ORG_TTL_S = 1.0, 3600.0  # the business's name (Clerk): bounded, and cached once read
DAYS = 7
_FACTS_MAX, _SLOTS_MAX, _BOOKINGS_MAX = 1200, 600, 200
_cache: dict[tuple[str, str | None], tuple[float, str]] = {}
_names: dict[str, tuple[float, str]] = {}


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ranges(starts: list[str]) -> str:
    """["…T09:00", "…T09:30", "…T14:00"] → "9:00 AM–9:30 AM, 2:00 PM" (starts every half hour, runs merged)."""
    out: list[list[datetime]] = []
    for t in (datetime.strptime(s, "%Y-%m-%dT%H:%M") for s in starts):
        if out and t - out[-1][1] == timedelta(minutes=30):
            out[-1][1] = t
        else:
            out.append([t, t])
    say = lambda t: t.strftime("%-I:%M %p")  # noqa: E731
    return ", ".join(say(a) if a == b else f"{say(a)}–{say(b)}" for a, b in out)


async def facts_text(ws: str, cfg: dict | None = None, *, max_chars: int | None = None) -> str:
    """The business's name, time zone and Agent page facts; "" with no saved Agent page (the phone's defaults are
    not facts). As on the phone (voice_phone.start_call), the facts are left out when the Agent page's "answer
    questions" is off or the line is paused. The explicit facts tool reads the complete saved facts;
    only the caller profile requests an excerpt."""
    from core.repositories import messaging_repo
    from core.services import clerk_admin
    from routers import voice_phone as vp

    if cfg is None:
        if not await messaging_repo.get_voice_agent(ws):
            return ""
        cfg = await vp.agent_config(ws)
    head = f"Business: {await _org_name(ws, clerk_admin) or 'not named'}. Time zone: {vp._tz(cfg).key}."
    if not cfg["allow"].get("answer") or cfg.get("paused"):
        return head
    facts = vp._facts(cfg)
    if max_chars is not None and len(facts) > max_chars:
        notice = "\n[Partial profile; complete saved answers are available from business_facts.]"
        facts = _cap(facts, max_chars - len(notice)) + notice
    return f"{head}\n{facts}"


async def _org_name(ws: str, clerk_admin) -> str:
    """The workspace's name from Clerk, ORG_S at most and cached ORG_TTL_S; "" when unknown or slow."""
    if not ws.startswith("org_"):
        return ""
    hit = _names.get(ws)
    if hit and time.monotonic() - hit[0] < ORG_TTL_S:
        return hit[1]
    try:
        org = await asyncio.wait_for(clerk_admin.get_organization(ws), ORG_S)
    except Exception:  # noqa: BLE001 -- the facts go out without the name rather than not at all
        logger.warning("business name unavailable ws=%s", ws, exc_info=True)
        return hit[1] if hit else ""
    name = (org or {}).get("name") or ""
    if name:
        _names[ws] = (time.monotonic(), name)
    return name


async def _open_days(ws: str, cfg: dict, now: datetime, minutes: int) -> str:
    """The week's open start times: the hours minus Nova's own bookings (all days at once), less Google's busy
    blocks after a verified bounded freeBusy; provider failure marks the section unknown."""
    from core.services import voice_booking
    from routers import voice_phone as vp

    dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS)]
    *taken, busy = await asyncio.gather(
        *(vp._taken(ws, d) for d in dates), voice_booking.busy_across(ws, cfg, dates, GOOGLE_S)
    )
    days = [
        f"{datetime.strptime(d, '%Y-%m-%d'):%a %-d %b}: {_ranges(s) if (s := vp.open_starts(cfg, d, t + busy, now, minutes, vp._lanes(cfg, ''))) else 'none'}"  # fmt: skip
        for d, t in zip(dates, taken, strict=True)
    ]
    return _cap(f"Open start times for a {minutes}-minute appointment: " + "; ".join(days), _SLOTS_MAX)


async def _callers_bookings(ws: str, caller: str, now: datetime) -> str:
    from routers import voice_phone as vp

    mine = [b for b in await _upcoming(ws, f"{now:%Y-%m-%dT%H:%M}") if b.get("phone") == caller]

    def describe(b: dict) -> str:
        text = f"{b['service']} {vp._pretty(vp._start(b))}" + (f" with {b['staff']}" if b.get("staff") else "")
        if b.get("pending"):
            pending = b["pending"]
            target = f"; requested {vp._pretty(pending['newStart'])}" if pending.get("newStart") else ""
            return f"{text} ({pending['action']} not confirmed{target})"
        if b.get("bookingState") == "held":
            return f"{text} (local hold; provider not confirmed)"
        return text

    said = "; ".join(describe(b) for b in mine) or "none"
    return _cap(f"This caller's upcoming bookings: {said}", _BOOKINGS_MAX)


async def _build(ws: str, caller: str | None) -> str:
    from core.repositories import messaging_repo
    from routers import voice_phone as vp

    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT_S
    async with asyncio.timeout_at(deadline):
        if not await messaging_repo.get_voice_agent(ws):
            return ""  # no saved Agent page: nothing is known, and the phone's defaults are not facts
        cfg = await vp.agent_config(ws)
    now, minutes = vp._now(cfg), vp._minutes(cfg, "")
    parts = [
        asyncio.create_task(facts_text(ws, cfg, max_chars=_FACTS_MAX)),
        asyncio.create_task(_open_days(ws, cfg, now, minutes)),
    ]
    if caller:
        parts.append(asyncio.create_task(_callers_bookings(ws, caller, now)))
    try:
        # One overall deadline, but independent outcomes: optional availability or
        # booking reads must not erase facts that have already been read.
        await asyncio.wait(parts, timeout=max(0, deadline - loop.time()))
    finally:
        for part in parts:
            if not part.done():
                part.cancel()
        facts, *rest = await asyncio.gather(*parts, return_exceptions=True)
    if isinstance(facts, asyncio.CancelledError):
        raise TimeoutError("business facts were not ready within the profile deadline")
    if isinstance(facts, BaseException):
        raise facts
    sections = [facts]
    for label, r in zip(("Open start times", "This caller's upcoming bookings"), rest):
        if isinstance(r, BaseException):
            logger.warning("business profile part unavailable ws=%s", ws, exc_info=r)
            sections.append(f"{label}: unknown (data unavailable).")
        else:
            sections.append(r)
    return "\n".join(sections)


async def _upcoming(ws: str, now: str) -> list[dict]:
    """`VOICEBOOK#` rows from now on (never the whole history)."""
    from boto3.dynamodb.conditions import Key

    from core.dynamodb import run_in_thread
    from core.repositories import messaging_repo

    r = await run_in_thread(
        messaging_repo._table().query,
        KeyConditionExpression=Key("pk").eq(messaging_repo.owner_pk(ws))
        & Key("sk").between(f"VOICEBOOK#{now}", "VOICEBOOK#\uffff"),
    )
    return r.get("Items", [])


def forget(ws: str) -> None:
    """Drop the workspace's cached profiles: a booking was made, moved or cancelled."""
    for key in [k for k in _cache if k[0] == ws]:
        _cache.pop(key, None)


async def business_profile(ws: str, caller: str | None = None) -> str:
    """The profile text, or "" when there is no Agent page or it could not be read in time. `caller`: the phone
    caller's number (their own bookings are added); None for a member. A caller's profile is never cached: the
    cache is per process, and a front_desk booking (API process) cannot drop the Router's copy (Temporal worker)."""
    key = (ws, caller)
    hit = None if caller else _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL_S:
        return hit[1]
    try:
        text = await _build(ws, caller)
    except Exception:  # noqa: BLE001 -- a turn never waits on or fails for the profile
        logger.warning("business profile unavailable ws=%s", ws, exc_info=True)
        return ""
    if not caller:
        _cache[key] = (time.monotonic(), text)
    return text


async def caller_of(trigger: dict | None) -> str | None:
    """The phone caller's number behind a `phone_contact` trigger (the call record start_call wrote) when the carrier
    attested it, else None."""
    ref = (trigger or {}).get("ref") or {}
    if ref.get("origin") != "phone_contact" or not ref.get("callId"):
        return None
    from core.repositories import messaging_repo

    from routers import voice_phone as vp

    meta = await messaging_repo.get_room(ref["callId"]) or {}
    call = meta.get("call") or {}
    # Only an A-attested number lists "This caller's upcoming bookings": a spoofed one would read the victim's.
    return call["caller"] if meta.get("kind") == "call" and vp._verified(call) else None
