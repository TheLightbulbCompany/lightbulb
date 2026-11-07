"""The owner's voice surfaces (spec 2026-11-07 §5): the Home timeline and the Agent page's settings.

`GET /api/v1/voice/timeline`: every call and meeting the workspace's assistant had, newest first. A call is a room
whose META has `kind == "call"` and its record under `META.call` (caller, callerName, returning, startedAt,
durationSec, outcome, summary, steps, transcript, callerMemory, recordingUrl, ownerMode, resolvedAt); a meeting is a
room with `META.meeting` (title, startAt, endAt, status, roster, plus the summary/asked/followUps the meeting agent
writes). `POST /api/v1/voice/calls/{roomId}/done` marks a call's Needs-you card handled.

`GET/PUT /api/v1/voice/agent`: the Agent page, one JSON document per workspace (any member reads, an org admin
writes). The page owns the defaults; an unset workspace reads `{"config": null}`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo

router = APIRouter()

#: The newest rooms read per timeline. ponytail: one META read per call/channel room, capped; page by createdAt
#: (or keep a by-kind index) when a workspace outgrows it.
TIMELINE_ROOMS = 300
MAX_CONFIG_BYTES = 64_000
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


@router.post("/voice/calls/{room_id}/done")
async def call_done(room_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = resolve_owner_id(auth)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != ws or meta.get("kind") != "call":
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    call = {**(meta.get("call") or {}), "resolvedAt": utc_now_iso(), "resolvedBy": auth.user_id}
    await messaging_repo.patch_room_meta(room_id, {"call": call}, expected_revision=int(meta["revision"]))
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
    await messaging_repo.put_voice_agent(resolve_owner_id(auth), body.config, auth.user_id)
    return {"config": body.config}
