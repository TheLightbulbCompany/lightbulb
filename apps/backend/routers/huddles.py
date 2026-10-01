"""Huddles: LiveKit rooms per channel, with the CEO as a summoned voice. See docs/superpowers/specs/2026-10-01-huddles-design.md.

Three auth models: Clerk for people (`/huddles/*`), the worker secret for the CEO worker
(`/internal/huddles/*`), and the call owner's hook token for the bridge (`/internal/calls/*/frames`).
"""

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import livekit_huddles, messaging, voice_calls
from core.services.live_events import get_live_event_publisher
from routers.messaging import _require_hook_token

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


async def _worker_call(call_id: str, token: str) -> dict:
    call = await voice_calls.get_call(call_id)
    if not call:
        raise _problem(404, "call_not_found")
    _require_room_token(token, call.get("roomId") or "", call["ownerId"])
    return call


class SummonBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    speakerUserId: _Id


class TurnBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    speakerUserId: _Id
    seq: int = Field(ge=1)
    utterance: str = Field(min_length=1, max_length=4_000)
    transcript: str = Field(default="", max_length=12_000)


class EndBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    usageSeconds: int = Field(ge=0)


class PresenceBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    userIds: list[_Id] = Field(max_length=128)
    version: int = Field(default=0, ge=0)  # monotonic per worker; a stale (reordered) snapshot is dropped


class FramesBody(BaseModel):
    events: list[dict[str, Any]] = Field(max_length=50)


@router.post("/huddles/{room_id}/join")
async def join(room_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    room_id = _room_id(room_id)
    if not livekit_huddles.configured():
        raise _problem(503, "huddles_unavailable")
    owner_id = resolve_owner_id(auth)
    # Member of the owner's own room (guests 404), not archived, at least one non-observe agent.
    await voice_calls._room_team(owner_id, auth.user_id, room_id)
    name = await messaging.human_display_name(auth.user_id)
    token = livekit_huddles.mint_token(room_id=room_id, user_id=auth.user_id, name=name)
    # Going Live is the earliest sign a voice turn is coming: start the workspace's cold start now.
    voice_calls.prewake_owner(owner_id)
    try:
        # The worker takes ~20 s to join: a second click (or person) in that window must not dispatch
        # another, which would evict the first. The window is shorter than the web's 45 s give-up, so
        # its Retry re-dispatches.
        if not await livekit_huddles.has_ceo(room_id) and await container_repo.try_claim_pass(
            f"huddle-dispatch:{room_id}", interval_s=DISPATCH_DEDUPE_S
        ):
            await livekit_huddles.dispatch_ceo(
                room_id,
                {"roomId": room_id, "ownerId": owner_id, "roomToken": livekit_huddles.room_token(room_id, owner_id)},
            )
    except Exception:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike: LiveKit is the upstream
        put_metric("huddle.livekit.error")
        logger.warning("huddle join: LiveKit call failed room=%s", room_id, exc_info=True)
        raise _problem(502, "huddles_upstream")
    return {"serverUrl": settings.LIVEKIT_URL, "roomName": livekit_huddles.room_name(room_id), "token": token}


@router.get("/huddles/{room_id}")
async def huddle(room_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    room_id = _room_id(room_id)
    owner_id = resolve_owner_id(auth)
    meta, _ = await messaging.require_room_member(owner_id, auth.user_id, room_id)
    if meta.get("ownerId") != owner_id:  # join's gate: a shared-channel guest sees no huddle
        raise _problem(404, "room_not_found")
    if not livekit_huddles.configured():
        return {"userIds": [], "available": False}  # dark: the client hides the Huddle button
    try:
        user_ids = await livekit_huddles.participants(room_id)
    except Exception:  # noqa: BLE001 -- every open channel header polls this; never a raw 500
        put_metric("huddle.livekit.error")
        logger.warning("huddle read: LiveKit call failed room=%s", room_id, exc_info=True)
        raise _problem(502, "huddles_upstream")
    return {"userIds": user_ids, "available": True}


@router.post("/internal/huddles/{room_id}/summon", dependencies=[_Worker])
async def summon(room_id: str, body: SummonBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    # start_huddle_call's _room_team 404s unless body.ownerId owns the room and the speaker is a member.
    return await voice_calls.start_huddle_call(
        owner_id=body.ownerId, room_id=room_id, speaker_user_id=body.speakerUserId
    )


@router.post("/internal/huddles/calls/{call_id}/turns", dependencies=[_Worker])
async def turn(call_id: str, body: TurnBody):
    await _worker_call(call_id, body.roomToken)
    # start_huddle_turn 404s unless the call is an open huddle owned by body.ownerId.
    return await voice_calls.start_huddle_turn(
        call_id=call_id,
        owner_id=body.ownerId,
        speaker_user_id=body.speakerUserId,
        seq=body.seq,
        utterance=body.utterance,
        transcript=body.transcript,
    )


@router.post("/internal/huddles/calls/{call_id}/end", dependencies=[_Worker])
async def end(call_id: str, body: EndBody):
    call = await _worker_call(call_id, body.roomToken)
    if call.get("kind") != "huddle" or call.get("ownerId") != body.ownerId:
        raise _problem(404, "call_not_found")
    return {"closed": await voice_calls.close_call(call_id, usage_seconds=body.usageSeconds)}


@router.post("/internal/huddles/{room_id}/presence", dependencies=[_Worker])
async def presence(room_id: str, body: PresenceBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("deletedAt") or meta.get("ownerId") != body.ownerId:
        raise _problem(404, "room_not_found")
    if body.version and not await container_repo.try_advance_huddle_presence(room_id, body.version):
        return {"ok": True, "stale": True}  # a newer snapshot already went out
    await get_live_event_publisher().publish_room(
        room_id,
        messaging.make_live_event("room.huddle.changed", {"roomId": room_id, "userIds": body.userIds}, room_id=room_id),
    )
    return {"ok": True}


@router.post("/internal/calls/{call_id}/frames")
async def frames(call_id: str, body: FramesBody, request: Request):
    call = await voice_calls.get_call(call_id)
    if not call:
        raise _problem(404, "call_not_found")
    await _require_hook_token(call["ownerId"], request)  # the call owner's token, never a caller-named owner
    if call.get("kind") != "huddle" or call.get("status") != voice_calls.OPEN:
        raise _problem(404, "call_not_found")
    await livekit_huddles.send_frames(call["roomId"], body.events)
    return {"ok": True}
