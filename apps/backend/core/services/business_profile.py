"""The business's own facts for the phone front door (router-latency-2026-11-08.md: 0 of 17 caller questions right,
because only the phone code could read them). A caller's Router batch and its Front desk thread get this block;
owner and member chat, the orb and Meet never do (founder, 8 Nov).

It reuses the phone's readers: the Agent page (`voice_phone.agent_config`, `_facts`: services, prices, durations,
hours, the Knowledge answers that hold the policies), open slots (`voice_booking.free`) and the caller's own
bookings (`VOICEBOOK#` rows with their number). Best-effort: bounded to ~2k characters, cached 60 s, 2 s at most.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TIMEOUT_S = 2.0
TTL_S = 60.0
DAYS = 7
_FACTS_MAX, _SLOTS_MAX, _BOOKINGS_MAX = 1200, 600, 200
_cache: dict[tuple[str, str | None], tuple[float, str]] = {}


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


async def facts_text(ws: str, cfg: dict | None = None) -> str:
    """The business's name, time zone and Agent page facts; "" with no saved Agent page (the phone's defaults are
    not facts). As on the phone (voice_phone.start_call), the facts are left out when the Agent page's "answer
    questions" is off or the line is paused. Shared by the caller profile and a Worker's `business_facts`."""
    from core.repositories import messaging_repo
    from core.services import clerk_admin
    from routers import voice_phone as vp

    if cfg is None:
        if not await messaging_repo.get_voice_agent(ws):
            return ""
        cfg = await vp.agent_config(ws)
    org = await clerk_admin.get_organization(ws) if ws.startswith("org_") else None
    head = f"Business: {(org or {}).get('name') or 'not named'}. Time zone: {vp._tz(cfg).key}."
    if not cfg["allow"].get("answer") or cfg.get("paused"):
        return head
    return f"{head}\n{_cap(vp._facts(cfg), _FACTS_MAX)}"


async def _build(ws: str, caller: str | None) -> str:
    from core.repositories import messaging_repo
    from core.services import voice_booking
    from routers import voice_phone as vp

    if not await messaging_repo.get_voice_agent(ws):
        return ""  # no saved Agent page: nothing is known, and the phone's defaults are not facts
    cfg = await vp.agent_config(ws)
    lines = [await facts_text(ws, cfg)]
    now, minutes = vp._now(cfg), vp._minutes(cfg, "")
    dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS)]
    # call_id None: a failed Google check here raises no Line problem (that is the call's own job)
    slots = await asyncio.gather(
        *(voice_booking.free(ws, cfg, None, d, minutes) for d in dates), return_exceptions=True
    )
    days = [
        f"{datetime.strptime(d, '%Y-%m-%d'):%a %-d %b}: {_ranges(s) if s else 'none'}"
        for d, s in zip(dates, slots, strict=True)
        if not isinstance(s, BaseException)
    ]
    if days:
        lines.append(_cap(f"Open start times for a {minutes}-minute appointment: " + "; ".join(days), _SLOTS_MAX))
    if caller:
        try:
            mine = [b for b in await _upcoming(ws, f"{now:%Y-%m-%dT%H:%M}") if b.get("phone") == caller]
            said = "; ".join(f"{b['service']} {vp._pretty(b['sk'][10:])}" for b in mine) or "none"
            lines.append(_cap(f"This caller's upcoming bookings: {said}", _BOOKINGS_MAX))
        except Exception:  # noqa: BLE001 -- the rest of the profile still helps
            logger.warning("caller bookings unavailable ws=%s", ws, exc_info=True)
    return "\n".join(lines)


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
    caller's number (their own bookings are added); None for a member."""
    key = (ws, caller)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL_S:
        return hit[1]
    try:
        text = await asyncio.wait_for(_build(ws, caller), TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- a turn never waits on or fails for the profile
        logger.warning("business profile unavailable ws=%s", ws, exc_info=True)
        return ""
    _cache[key] = (time.monotonic(), text)
    return text


async def caller_of(trigger: dict | None) -> str | None:
    """The phone caller's number behind a `phone_contact` trigger (the call record start_call wrote), else None."""
    ref = (trigger or {}).get("ref") or {}
    if ref.get("origin") != "phone_contact" or not ref.get("callId"):
        return None
    from core.repositories import messaging_repo

    meta = await messaging_repo.get_room(ref["callId"]) or {}
    return ((meta.get("call") or {}).get("caller") or None) if meta.get("kind") == "call" else None
