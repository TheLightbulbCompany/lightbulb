"""The owner's voice surfaces (spec 2026-11-07 §5): the Home timeline and the Agent page's settings.

`GET /api/v1/voice/timeline`: every call and meeting the workspace's assistant had, newest first. A call is a room
whose META has `kind == "call"` and its record under `META.call` (caller, callerName, returning, startedAt,
durationSec, outcome, summary, steps, transcript, callerMemory, recordingUrl, ownerMode, resolvedAt); a meeting is a
room with `META.meeting` (title, startAt, endAt, status, roster, plus the summary/asked/followUps the meeting agent
writes). `POST /api/v1/voice/calls/{roomId}/done` marks a call's Needs-you card handled.

`GET/PUT /api/v1/voice/agent`: the Agent page, one JSON document per workspace (any member reads, an org admin
writes). The page owns the defaults; an unset workspace reads `{"config": null}` and the page opens onboarding.

Onboarding (admin): `POST /voice/onboarding/draft` drafts the Agent page from the business's website;
`POST /voice/number` stores the number choice (keep and forward, or new); `PUT /voice/agent` with `live: true` gives the
number at Go live (core/services/voice_onboarding.py).
"""

from __future__ import annotations

import asyncio
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.dynamodb import run_in_thread, utc_now_iso
from core.billing import voice_billing
from core.repositories import messaging_repo
from core.services import voice_onboarding, voice_outbound

router = APIRouter()

#: The newest rooms read per timeline. ponytail: one META read per call/channel room, capped; page by createdAt
#: (or keep a by-kind index) when a workspace outgrows it.
TIMELINE_ROOMS = 300
MAX_CONFIG_BYTES = 64_000
#: What a live greeting must say (the frontend's `isLawful`): an empty greeting is the page's default, which says both.
_E164 = re.compile(r"\+[1-9]\d{6,14}")  # routers/agent_text._valid_phone


def _e164(raw: str) -> str | None:
    """The handoff number as the worker dials it (tel:+…), from what the page's field takes ("(415) 555-0147").
    ponytail: a number without + is read as US (+1); other countries type the +."""
    digits = re.sub(r"\D", "", raw)
    if not raw.strip().startswith("+"):
        digits = "1" + digits if len(digits) == 10 else digits if len(digits) == 11 and digits[0] == "1" else ""
    number = f"+{digits}"
    return number if _E164.fullmatch(number) else None


#: Meeting fields the page shows. Never the ticket nonce or the Recall bot id.
_MEETING_FIELDS = ("title", "startAt", "endAt", "status", "summary", "asked", "followUps", "notesSentAt")


def _epoch_ms(v: Any) -> int:
    """An ISO string, epoch seconds or epoch ms, as epoch ms (0 when missing or unreadable)."""
    if isinstance(v, (int, float)):
        return int(v if v > 1e12 else v * 1000)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _item(meta: dict) -> dict | None:
    if meta.get("deletedAt"):
        return None
    if meta.get("kind") == "call":
        call = meta.get("call") or {}
        return {
            **call,
            "type": "call",
            "roomId": meta["roomId"],
            "at": _epoch_ms(call.get("startedAt") or meta["createdAt"]),
        }
    meeting = meta.get("meeting")
    if meeting:
        roster = meeting.get("roster") or {}
        return {
            **{k: meeting[k] for k in _MEETING_FIELDS if k in meeting},
            "type": "meeting",
            "roomId": meta["roomId"],
            "people": [p["name"] for p in roster.values() if p.get("name")],
            "at": _epoch_ms(meeting.get("startAt") or meta["createdAt"]),
        }
    return None


@router.get("/voice/timeline")
async def timeline(auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = resolve_owner_id(auth)
    rows = [r for r in await messaging_repo.list_owner_rooms(ws) if r.get("kind") in ("call", "channel")]
    rows = sorted(rows, key=lambda r: r.get("createdAt") or "", reverse=True)[:TIMELINE_ROOMS]
    metas = await asyncio.gather(*(messaging_repo.get_room(r["roomId"]) for r in rows))
    items = [i for m in metas if m and m.get("ownerId") == ws and (i := _item(m))]
    return {"items": sorted(items, key=lambda i: i["at"], reverse=True)}


async def month_seconds(ws: str, tz_name: str | None) -> int:
    """Seconds of calls and held meetings since the 1st of this month in the business's timezone. Calls are made
    when they start, so last month's are skipped unread; every channel is read (a meeting is booked ahead).
    ponytail: one META read per room this month, and a series counts only its newest occurrence (history keeps no
    inCallAt); a per-month counter row written at call end when either matters."""
    try:
        tz = ZoneInfo(tz_name or "America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("America/New_York")
    start = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    floor = (start - timedelta(days=1)).strftime("%Y-%m-%d")  # createdAt is UTC ISO: a day of slack for the zone
    rows = [r for r in await messaging_repo.list_owner_rooms(ws)
            if r.get("kind") == "channel" or (r.get("kind") == "call" and (r.get("createdAt") or "") >= floor)]  # fmt: skip
    total, since = 0, start.timestamp()
    for m in await asyncio.gather(*(messaging_repo.get_room(r["roomId"]) for r in rows)):
        if not m or m.get("ownerId") != ws:
            continue
        if m.get("kind") == "call":
            call = m.get("call") or {}
            if _epoch_ms(call.get("startedAt") or m["createdAt"]) >= since * 1000:
                total += int(call.get("durationSec") or 0)
        elif (mt := m.get("meeting") or {}).get("inCallAt") and mt.get("endedAt") and int(mt["inCallAt"]) >= since:
            total += max(0, int(mt["endedAt"]) - int(mt["inCallAt"]))
    return total


@router.get("/voice/usage")
async def usage(auth: AuthContext = Depends(get_current_user)) -> dict:
    """The Settings "Billing & minutes" card: this month's minutes, the plan, and what happens when they run out."""
    ws = resolve_owner_id(auth)
    cfg = await messaging_repo.get_voice_agent(ws) or {}
    return {
        "used": round(await month_seconds(ws, cfg.get("timezone")) / 60),
        "included": voice_billing.INCLUDED_MINUTES,
        "overageRate": voice_billing.OVERAGE_USD_PER_MINUTE,
        "runOut": "forward" if cfg.get("runOut") == "forward" else "keep",
    }


@router.post("/voice/calls/{room_id}/done")
async def call_done(room_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = resolve_owner_id(auth)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != ws or meta.get("kind") != "call":
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    call = {**(meta.get("call") or {}), "resolvedAt": utc_now_iso(), "resolvedBy": auth.user_id}
    await messaging_repo.patch_room_meta(room_id, {"call": call}, expected_revision=int(meta["revision"]))
    return {"ok": True}


@router.delete("/voice/callers/{e164}/memory")
async def forget_caller(e164: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Forget: clear what the assistant remembers of one caller (facts and name)."""
    number, ws = _e164(e164), resolve_owner_id(auth)
    key = {"pk": messaging_repo.owner_pk(ws), "sk": f"VOICECALLER#{number}"}
    if not number or not await messaging_repo._get(**key):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    await run_in_thread(
        messaging_repo._table().update_item, Key=key, UpdateExpression="SET facts = :f, forgottenAt = :t REMOVE #n",
        ExpressionAttributeNames={"#n": "name"}, ExpressionAttributeValues={":f": [], ":t": utc_now_iso()},
    )  # fmt: skip
    return {"ok": True}


class AgentConfigIn(BaseModel):
    config: dict


@router.get("/voice/agent")
async def get_agent(auth: AuthContext = Depends(get_current_user)) -> dict:
    return {"config": await messaging_repo.get_voice_agent(resolve_owner_id(auth))}


@router.put("/voice/agent")
async def put_agent(body: AgentConfigIn, auth: AuthContext = Depends(get_current_user)) -> dict:
    require_org_admin(auth)
    if len(json.dumps(body.config)) > MAX_CONFIG_BYTES:
        raise HTTPException(status_code=413, detail={"code": "too_large"})
    tz = body.config.get("timezone")
    if tz:
        try:
            ZoneInfo(str(tz))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "timezone_invalid"}) from exc
    handoff = body.config.get("handoff")
    if isinstance(handoff, dict) and handoff.get("number"):
        number = _e164(handoff["number"]) if isinstance(handoff["number"], str) else None
        if number is None:  # the worker dials tel:{number} as stored
            raise HTTPException(status_code=422, detail={"code": "handoff_number_invalid"})
        body.config["handoff"] = {**handoff, "number": number}
    ws = resolve_owner_id(auth)
    if body.config.get("live") and not (await messaging_repo.get_voice_agent(ws) or {}).get("live"):
        try:  # Go live: the number is given (bought, once buying is on) now, never at sign-up
            body.config["number"] = await voice_onboarding.provision_at_go_live(ws, body.config, auth.user_id)
        except voice_onboarding.NoNumber as exc:
            raise HTTPException(status_code=409, detail={"code": "number_unavailable", "reason": str(exc)}) from None
    await messaging_repo.put_voice_agent(ws, body.config, auth.user_id)
    return {"config": body.config}


class ForwardCheckIn(BaseModel):
    forwardFrom: str = Field(default="", max_length=40)


@router.post("/voice/forward-check")
async def forward_check(body: ForwardCheckIn | None = None, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Onboarding "Keep my number": ring the business line once; the page polls GET /voice/agent for forwardingOk.
    `forwardFrom` is the line the owner just typed (step 4 saves only on Continue); it is kept on the document."""
    require_org_admin(auth)
    ws = resolve_owner_id(auth)
    cfg = await messaging_repo.get_voice_agent(ws) or {}
    if body and body.forwardFrom:
        cfg["forwardFrom"] = body.forwardFrom
    return await voice_outbound.forward_check(ws, cfg)


class DraftIn(BaseModel):
    website: str = Field(min_length=3, max_length=300)
    business: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=120)


@router.post("/voice/onboarding/draft")
async def onboarding_draft(body: DraftIn, auth: AuthContext = Depends(get_current_user)) -> dict:
    require_org_admin(auth)
    return {"draft": await voice_onboarding.draft(body.website, body.business, body.city)}


class NumberIn(BaseModel):
    path: Literal["keep", "new"] | None = None  # None: today's step 4 (posts {})
    areaCode: str | None = Field(default=None, pattern=r"^[2-9]\d{2}$")
    carrier: str | None = Field(default=None, pattern=r"^[a-z_]{1,30}$")
    mode: Literal["missed", "all"] | None = None


@router.post("/voice/number")
async def assign_number(body: NumberIn | None = None, auth: AuthContext = Depends(get_current_user)) -> dict:
    """With a body: store the onboarding choice (keep and forward, or new), nothing bought. Without: today's step 4,
    the configured line now. ponytail: the bodyless branch goes when Task 30's step 4 sends the choice."""
    require_org_admin(auth)
    if body and body.path:
        ws = resolve_owner_id(auth)
        choice = await voice_onboarding.reserve(
            ws, body.path, body.areaCode, body.carrier, body.mode, user_id=auth.user_id
        )
        return {"numberChoice": choice}
    try:
        return {"number": await voice_onboarding.assign_number(resolve_owner_id(auth), auth.user_id)}
    except voice_onboarding.NoNumber as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None
