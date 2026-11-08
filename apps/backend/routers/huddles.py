"""Huddles ("Live"): LiveKit rooms for every room kind, with Lightbulb (the CEO worker) as a summoned voice. See docs/superpowers/specs/2026-10-01-huddles-design.md.

Two auth models: Clerk for people (`/huddles/*`) and the worker secret for the voice worker
(`/internal/huddles/{roomId}/transcript`).
"""

import contextlib
import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import livekit_huddles, meeting_invites, meeting_notes, messaging, voice_calls

logger = logging.getLogger(__name__)
router = APIRouter()

_Id = Annotated[str, Field(min_length=1, max_length=128)]
DISPATCH_DEDUPE_S = 40


def _problem(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _require_worker(x_lightbulb_huddle_secret: Annotated[str, Header()] = "") -> None:
    expected = settings.HUDDLE_WORKER_SECRET
    # An unset secret must never match an empty header.
    if not expected or not hmac.compare_digest(x_lightbulb_huddle_secret.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid huddle worker secret")


_Worker = Depends(_require_worker)


def _require_room_token(presented: str, room_id: str, owner_id: str) -> None:
    if not hmac.compare_digest(presented.encode(), livekit_huddles.room_token(room_id, owner_id).encode()):
        raise HTTPException(status_code=401, detail="invalid huddle room token")


def _room_id(room_id: str) -> str:
    # One canonical (lowercase) id per channel: an uppercase path must not open a second LiveKit room.
    return messaging.validate_uuid(room_id, label="room ID")


class RoomBody(BaseModel):
    ownerId: _Id
    roomToken: _Id


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
    if not livekit_huddles.configured():
        raise _problem(503, "huddles_unavailable")
    owner_id = resolve_owner_id(auth)
    # Member of the owner's own room (guests 404), not archived. Any room kind; with no agent
    # member it is a people-only voice room: no worker dispatch, no runtime wake.
    meta, team = await voice_calls._room_team(owner_id, auth.user_id, room_id, need_team=False)
    in_call = HTTPException(
        status_code=409,
        detail={"code": "meeting_in_call", "message": "This room's meeting is live. Join it in Google Meet."},
    )
    # A meeting room's join runs under the meeting's lease, on a META read taken inside it, so it
    # cannot interleave with the meeting going in_call: a Live worker never shares a room with one.
    lease = meeting_invites.meeting_lease(room_id) if meta.get("meeting") else contextlib.nullcontext()
    try:
        async with lease:
            if meta.get("meeting"):
                meta = await messaging_repo.get_room(room_id) or meta
            if (meta.get("meeting") or {}).get("status") == "in_call":
                raise in_call
            return await _join(room_id, owner_id, auth.user_id, team, owner_mode=meta.get("kind") == "dm")
    except meeting_invites.MeetingBusy:
        raise in_call from None


async def _join(room_id: str, owner_id: str, user_id: str, team: list[dict], *, owner_mode: bool = False) -> dict:
    # Agent voice lives only in a DM with the assistant (owner mode, routers/voice_owner.py): the worker's
    # multi-voice huddle engine for other rooms is gone (task 9a). A people-only room still joins.
    if team and not owner_mode:
        from routers.voice_meet import assistant_name  # voice_meet imports this module

        raise HTTPException(
            status_code=409,
            detail={
                "code": "voice_in_dm_only",
                "message": f"Voice lives in your room with {await assistant_name(owner_id)}.",
            },
        )
    name = await messaging.human_display_name(user_id)
    token = livekit_huddles.mint_token(room_id=room_id, user_id=user_id, name=name)
    out = {"serverUrl": settings.LIVEKIT_URL, "roomName": livekit_huddles.room_name(room_id), "token": token}
    if not team:
        return {**out, "lightbulb": False}
    await voice_calls._gate(owner_id)  # the plan and free-minutes check /summon makes: 402 voice_limit
    try:
        # The worker takes ~20 s to join: a second click (or person) in that window must not dispatch
        # another, which would evict the first. The window is shorter than the web's 45 s give-up, so
        # its Retry re-dispatches.
        if not await livekit_huddles.has_ceo(room_id, live=True) and await container_repo.try_claim_pass(
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


@router.get("/huddles/{room_id}")
async def huddle(room_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    room_id = _room_id(room_id)
    owner_id = resolve_owner_id(auth)
    meta, _ = await messaging.require_room_member(owner_id, auth.user_id, room_id)
    if meta.get("ownerId") != owner_id:  # join's gate: a shared-channel guest sees no huddle
        raise _problem(404, "room_not_found")
    # False: a people-only room; the web hides Lightbulb's visualizer and status.
    lightbulb = bool(await voice_calls.room_agents(room_id))
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
    read back. A meeting's ``final`` request carries the whole transcript and becomes its room's
    ``transcript.txt``; nothing else is posted to the room."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id) or {}
    if meta.get("meeting") and meta.get("ownerId") == body.ownerId:
        await meeting_notes.on_transcript(room_id, [x.model_dump(exclude={"ready"}) for x in body.lines], body.final)
    rendered = "\n".join(
        f"[{x.mark} @{int(x.at)}{f' waited {x.at - x.ready:.1f}s' if x.ready else ''}] "
        f"{x.name}{' → ' + x.to if x.to else ''}: {x.text}"
        for x in body.lines
    )
    logger.info("live transcript room=%s huddle=%s owner=%s\n%s", room_id, body.huddleId, body.ownerId, rendered)
    put_metric("voice.huddle.transcript_lines", len(body.lines))
    return {"ok": True}
