"""Huddles ("Live"): LiveKit rooms for every room kind, with Lightbulb (the CEO worker) as a summoned voice. See docs/superpowers/specs/2026-10-01-huddles-design.md.

Two auth models: Clerk for people (`/huddles/*`) and the worker secret for the voice worker
(`/internal/huddles/{roomId}/transcript`).
"""

import contextlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, require_org, resolve_owner_id
from core.config import settings
from core.engine import api
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services import clerk_admin
from core.services.person_label import email_label
from core.voice import livekit_huddles, rooms
from core.voice.meetings import meeting_invites, meeting_notes
from core.voice.worker_api import _Id, _problem, _require_room_token, _room_id, _Worker

logger = logging.getLogger(__name__)
router = APIRouter()

DISPATCH_DEDUPE_S = 40


class TranscriptLine(BaseModel):
    mark: int = Field(ge=0)
    kind: str = Field(max_length=8)  # person | agent
    name: str = Field(max_length=200)
    text: str = Field(max_length=4000)
    at: float = 0.0
    to: str = Field(default="", max_length=200)
    ready: float = 0.0  # an agent line: when the answer it opens was ready (the worker's ready -> heard wait)


class TranscriptBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    huddleId: _Id
    lines: list[TranscriptLine] = Field(max_length=5_000)
    final: bool = False  # a meeting's whole transcript, in one request

    @model_validator(mode="after")
    def _bounded(self):
        if not self.final and len(self.lines) > 500:  # rolling flushes stay small; only `final` is whole
            raise ValueError("a non-final transcript batch holds at most 500 lines")
        return self


@router.post("/huddles/{room_id}/join")
async def join(room_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    room_id = _room_id(room_id)
    require_org(auth)  # a LiveKit room and the voice worker
    if not livekit_huddles.configured():
        raise _problem(503, "huddles_unavailable")
    owner_id = resolve_owner_id(auth)
    # Member of the owner's own room (guests 404), not archived. Any room kind; with no agent
    # member it is a people-only voice room: no worker dispatch, no runtime wake.
    conv, team = await rooms._room_team(owner_id, auth.user_id, room_id, need_team=False)
    in_call = HTTPException(
        status_code=409,
        detail={"code": "meeting_in_call", "message": "This room's meeting is live. Join it in Google Meet."},
    )
    # A meeting room's join runs under the meeting's lease, on a META read taken inside it, so it
    # cannot interleave with the meeting going in_call: a Live worker never shares a room with one.
    meeting = conv.annotations.get("meeting")
    lease = meeting_invites.meeting_lease(room_id) if meeting else contextlib.nullcontext()
    try:
        async with lease:
            if meeting:
                meeting = ((await api.get_conversation(owner_id, room_id)) or conv).annotations.get("meeting")
            if (meeting or {}).get("status") == "in_call":
                raise in_call
            return await _join(room_id, owner_id, auth.user_id, team, owner_mode=conv.kind == "dm")
    except meeting_invites.MeetingBusy:
        raise in_call from None


async def _join(room_id: str, owner_id: str, user_id: str, team: list[dict], *, owner_mode: bool = False) -> dict:
    # Agent voice lives only in a DM with the assistant (owner mode, routers/voice_owner.py): the worker's
    # multi-voice huddle engine for other rooms is gone (task 9a). A people-only room still joins.
    if team and not owner_mode:
        from core.voice.room_lines import assistant_name

        raise HTTPException(
            status_code=409,
            detail={
                "code": "voice_in_dm_only",
                "message": f"Voice lives in your room with {await assistant_name(owner_id)}.",
            },
        )
    name = await _display_name(user_id)
    token = livekit_huddles.mint_token(room_id=room_id, user_id=user_id, name=name)
    out = {"serverUrl": settings.LIVEKIT_URL, "roomName": livekit_huddles.room_name(room_id), "token": token}
    if not team:
        return {**out, "lightbulb": False}
    await rooms._gate(owner_id)  # the plan and free-minutes check /summon makes: 402 voice_limit
    try:
        # The worker takes ~20 s to join: a second click (or person) in that window must not dispatch
        # another, which would evict the first. The window is shorter than the web's 45 s give-up, so
        # its Retry re-dispatches.
        if not await livekit_huddles.has_ceo(owner_id, room_id, live=True) and await container_repo.try_claim_pass(
            f"huddle-dispatch:{room_id}", interval_s=DISPATCH_DEDUPE_S
        ):
            await livekit_huddles.dispatch_ceo(
                room_id,
                {
                    "roomId": room_id,
                    "ownerId": owner_id,
                    "roomToken": livekit_huddles.room_token(room_id, owner_id),
                    "mode": "owner",
                },
            )
    except Exception:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike: LiveKit is the upstream
        put_metric("huddle.livekit.error")
        logger.warning("huddle join: LiveKit call failed room=%s", room_id, exc_info=True)
        raise _problem(502, "huddles_upstream")
    return {**out, "lightbulb": True}


async def _display_name(user_id: str) -> str:
    """The person's name as the app shows it: Clerk's first and last name, else username, else the email's local
    part, else the id."""
    user = await clerk_admin.get_user(user_id) or {}
    name = " ".join(p.strip() for p in (user.get("first_name") or "", user.get("last_name") or "") if p.strip())
    emails = user.get("email_addresses") or [{}]
    return (name or str(user.get("username") or "") or email_label(emails[0].get("email_address")) or user_id)[:256]


@router.get("/huddles/{room_id}")
async def huddle(room_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    room_id = _room_id(room_id)
    owner_id = resolve_owner_id(auth)
    if await api.http(api.get_conversation(owner_id, room_id, member=auth.user_id)) is None:
        raise _problem(404, "room_not_found")  # join's gate: a shared-channel guest sees no huddle
    # False: a people-only room; the web hides Lightbulb's visualizer and status.
    lightbulb = bool(await rooms.room_agents(owner_id, room_id))
    if not livekit_huddles.configured():
        return {"userIds": [], "available": False, "lightbulb": lightbulb}  # dark: the client hides Go live
    try:
        user_ids = await livekit_huddles.participants(room_id)
    except Exception:  # noqa: BLE001 -- every open channel header polls this; never a raw 500
        put_metric("huddle.livekit.error")
        logger.warning("huddle read: LiveKit call failed room=%s", room_id, exc_info=True)
        raise _problem(502, "huddles_upstream")
    return {"userIds": user_ids, "available": True, "lightbulb": lightbulb}


@router.post("/internal/huddles/{room_id}/transcript", dependencies=[_Worker])
async def transcript(room_id: str, body: TranscriptBody):
    """The worker's rolling transcript, in batches: what people and agents said in a huddle, kept
    in the backend's CloudWatch log (search `live transcript huddle=<id>`) so a bad call can be
    read back. A meeting's ``final`` request carries the whole transcript, which is kept in the Meetings
    transcript store for its notes and the meeting view; nothing is posted to the room. The batch is logged
    before that write, so a failed write (an error answer the worker retries) still leaves the log copy."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    rendered = "\n".join(
        f"[{x.mark} @{int(x.at)}{f' waited {x.at - x.ready:.1f}s' if x.ready else ''}] "
        f"{x.name}{' → ' + x.to if x.to else ''}: {x.text}"
        for x in body.lines
    )
    logger.info("live transcript room=%s huddle=%s owner=%s\n%s", room_id, body.huddleId, body.ownerId, rendered)
    put_metric("voice.huddle.transcript_lines", len(body.lines))
    conv = await api.get_conversation(body.ownerId, room_id)
    if conv and conv.annotations.get("meeting"):
        lines = [x.model_dump(exclude={"ready"}) for x in body.lines]
        await meeting_notes.on_transcript(body.ownerId, room_id, lines, body.final)
    return {"ok": True}
