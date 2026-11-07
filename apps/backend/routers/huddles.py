"""Huddles ("Live"): LiveKit rooms for every room kind, with Lightbulb (the CEO worker) as a summoned voice. See docs/superpowers/specs/2026-10-01-huddles-design.md.

Three auth models: Clerk for people (`/huddles/*`), the worker secret for the CEO worker
(`/internal/huddles/*`), and the owner's hook token for the bridge's frame relay
(`/internal/huddles/{ownerId}/rooms/{roomId}/frames`).
"""

import contextlib
import hmac
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import livekit_huddles, meeting_invites, meeting_notes, messaging, voice_calls
from core.services.live_events import get_live_event_publisher
from routers.messaging import _require_hook_token

logger = logging.getLogger(__name__)
router = APIRouter()

_Id = Annotated[str, Field(min_length=1, max_length=128)]
# A Meet speaker's label ("Priya", "Priya + Dan"): shown in a meeting room, never used to authorize.
_SpeakerName = Annotated[str | None, Field(max_length=120)]
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


class RoomBody(BaseModel):
    ownerId: _Id
    roomToken: _Id


class SummonBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    speakerUserId: _Id
    speakerName: _SpeakerName = None


class TurnBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    speakerUserId: _Id
    speakerName: _SpeakerName = None
    agentId: _Id
    runId: uuid.UUID  # the worker's, once per delegation and resent on every retry
    utterance: str = Field(min_length=1, max_length=4_000)
    transcript: str = Field(default="", max_length=12_000)
    steerRunId: uuid.UUID | None = None
    steerOnly: bool = False  # steer steerRunId or do nothing: lines said in the room never start a run (v2 §4.3)
    # The same spoken turn's other asks ("Forge is answering ..."), for a new run's input (engine §3.4).
    requests: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=8)


class RouteBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    speakerUserId: _Id
    utterance: str = Field(min_length=1, max_length=4_000)
    transcript: str = Field(default="", max_length=12_000)
    humans: int | None = Field(default=None, ge=0, le=128)  # people in the call; None: the Lightbulb wake word
    # Live's open requests for Jev (routing v2 §5): {agent, speaker} holding the floor, [{agent, request}].
    # ``mine``: the speaker is the person the floor agent is talking with (understanding §4.4).
    floor: dict[str, str | bool] | None = None
    voiceRuns: list[dict[str, str]] = Field(default_factory=list, max_length=16)  # a worker before ``open``
    # The worker's request list (understanding §4.4): open rows and the newest answered ones, each with its
    # ``asker`` and how far its answer has been said (``delivery``).
    open: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    # Who the speaker is talking with, one per agent, newest first: {agentId, obligation, task, state, expects,
    # why, last, lastBy, seconds}. What a meeting line with no name on it is routed on (voice_calls._engaged).
    engagements: list[dict[str, str | int]] = Field(default_factory=list, max_length=8)
    # Epoch seconds the transcript's last lines were said, oldest first, aligned to its end (Live reask rule).
    at: list[int] = Field(default_factory=list, max_length=30)
    setting: Literal["live", "meeting"] = "live"  # "meeting": a Google Meet, stricter floor
    unclaimed: bool = False  # a question to the room nobody took for 3.5 s (the worker measures it)


class EndBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    usageSeconds: int = Field(ge=0)


class PresenceBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    userIds: list[_Id] = Field(max_length=128)
    version: int = Field(default=0, ge=0)  # monotonic per worker; a stale (reordered) snapshot is dropped


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


class LateAnswerBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
    agentId: _Id
    runId: _Id
    request: str = Field(default="", max_length=2000)
    answer: str = Field(max_length=20000)


class FramesBody(BaseModel):
    events: list[dict[str, Any]] = Field(max_length=50)


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
    name = await messaging.human_display_name(user_id)
    token = livekit_huddles.mint_token(room_id=room_id, user_id=user_id, name=name)
    out = {"serverUrl": settings.LIVEKIT_URL, "roomName": livekit_huddles.room_name(room_id), "token": token}
    if not team:
        return {**out, "lightbulb": False}
    # A DM with the assistant is owner mode (routers/voice_owner.py): the worker runs the Router session, whose
    # reads and start_work go through the backend, so nothing below is needed. Elsewhere going Live is the
    # earliest sign a voice turn is coming: start the workspace's cold start now, and warm Jev's agent cards so
    # the first /route fits its 3 s budget (F4).
    if owner_mode:
        await voice_calls._gate(owner_id)  # the plan and free-minutes check /summon makes: 402 voice_limit
    else:
        voice_calls.prewake_owner(owner_id, team, room_id)
        voice_calls.prewarm_cards(owner_id, team)
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
                    **({"mode": "owner"} if owner_mode else {}),
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


@router.post("/internal/huddles/{room_id}/summon", dependencies=[_Worker])
async def summon(room_id: str, body: SummonBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    # open_voice_window's _room_team 404s unless body.ownerId owns the room and the speaker is a member.
    try:
        return await voice_calls.open_voice_window(
            owner_id=body.ownerId, room_id=room_id, speaker_user_id=body.speakerUserId
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "voice_limit":  # a Meet's people learn it in the meeting's chat
            voice_calls._fire(meeting_invites.say_voice_limit(room_id, detail.get("reason")))
        raise


@router.post("/internal/huddles/{room_id}/team", dependencies=[_Worker])
async def team(room_id: str, body: RoomBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    return await voice_calls.huddle_team(owner_id=body.ownerId, room_id=room_id)


@router.post("/internal/huddles/{room_id}/agents/{agent_id}/session", dependencies=[_Worker])
async def agent_session(room_id: str, agent_id: str, body: RoomBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    return await voice_calls.agent_session(owner_id=body.ownerId, room_id=room_id, agent_id=agent_id)


@router.post("/internal/huddles/{room_id}/agents/{agent_id}/meet-voice", dependencies=[_Worker])
async def agent_meet_voice(room_id: str, agent_id: str, body: RoomBody):
    """The voice that reads this agent's ready answer in a Meet. Not `/session`: that one claims the one-time
    "what should I call you" prompt, which a read-aloud answer never asks."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    return await voice_calls.agent_meet_voice(owner_id=body.ownerId, room_id=room_id, agent_id=agent_id)


@router.post("/internal/huddles/calls/{call_id}/turns", dependencies=[_Worker])
async def turn(call_id: str, body: TurnBody):
    call = await _worker_call(call_id, body.roomToken)
    if call.get("kind") != "huddle" or call.get("ownerId") != body.ownerId or call.get("status") != voice_calls.OPEN:
        raise _problem(404, "call_not_found")
    args = {
        "call": call,
        "agent_id": body.agentId,
        "speaker_id": body.speakerUserId,
        "speaker_name": body.speakerName,
        "utterance": body.utterance,
    }
    if body.steerRunId:
        steered = await voice_calls.steer_voice_run(run_id=str(body.steerRunId), **args)
        if steered:
            return steered
    if body.steerOnly:
        return {"runId": str(body.steerRunId), "agentId": body.agentId, "steered": False}
    return await voice_calls.dispatch_voice_run(
        run_id=str(body.runId), transcript=body.transcript, requests=body.requests, **args
    )


@router.post("/internal/huddles/{room_id}/route", dependencies=[_Worker])
async def route(room_id: str, body: RouteBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    if body.setting == "meeting" and not ((await messaging_repo.get_room(room_id)) or {}).get("meeting"):
        raise _problem(400, "not_a_meeting")  # the meeting floor never runs in a Live room
    args = {
        "owner_id": body.ownerId,
        "room_id": room_id,
        "speaker_id": body.speakerUserId,
        "utterance": body.utterance,
        "transcript": body.transcript,
    }
    # One router (routing v2 §6). humans None is the Lightbulb wake word: someone answers. Otherwise a
    # null agentId is nobody (people talking to each other); "hold" asks the worker to wait for the
    # speaker's next final (an older worker ignores it and reads null as nobody).
    return await voice_calls.route_live_line(
        **args,
        humans=body.humans or 0,
        wake=body.humans is None,
        floor=body.floor,
        voice_runs=body.voiceRuns,
        at=body.at,
        setting=body.setting,
        unclaimed=body.unclaimed,
        open_requests=body.open,
        engagements=body.engagements,
    )


@router.post("/internal/huddles/{room_id}/runs/{run_id}/abort", dependencies=[_Worker])
async def abort(room_id: str, run_id: str, body: RoomBody):
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    return {"aborted": await voice_calls.abort_voice_run(body.ownerId, room_id, run_id)}


@router.post("/internal/huddles/{room_id}/runs/{run_id}/no-post", dependencies=[_Worker])
async def no_post(room_id: str, run_id: str, body: RoomBody):
    """ "Don't post that": the run's answer never reaches the room late, by either door."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    return {"noPost": await voice_calls.mark_no_post(body.ownerId, room_id, run_id)}


@router.post("/internal/huddles/calls/{call_id}/end", dependencies=[_Worker])
async def end(call_id: str, body: EndBody):
    call = await _worker_call(call_id, body.roomToken)
    if call.get("kind") != "huddle" or call.get("ownerId") != body.ownerId:
        raise _problem(404, "call_not_found")
    return {"closed": await voice_calls.close_call(call_id, usage_seconds=body.usageSeconds)}


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


@router.post("/internal/huddles/{room_id}/late-answer", dependencies=[_Worker])
async def late_answer(room_id: str, body: LateAnswerBody):
    """A voice run's answer nobody heard -- it finished after the asker moved on, the news
    expired, or the call ended -- is posted into the room by its agent instead of dropped (a
    meeting keeps it on its record for the notes and the mail)."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    await voice_calls.post_late_answer(body.ownerId, room_id, body.agentId, body.runId, body.request, body.answer)
    return {"ok": True}


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


@router.post("/internal/huddles/{owner_id}/rooms/{room_id}/frames")
async def frames(owner_id: str, room_id: str, body: FramesBody, request: Request):
    """The bridge's voice-run frames (C2), relayed to the room's worker. Not tied to the window:
    a background run keeps relaying after Live closes (no worker -> dropped, 200)."""
    await _require_hook_token(owner_id, request)
    room_id = _room_id(room_id)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != owner_id:
        raise _problem(404, "room_not_found")
    if any((ev.get("payload") or {}).get("roomId") != room_id for ev in body.events):
        raise _problem(400, "room_mismatch")
    try:
        await livekit_huddles.send_frames(room_id, body.events)
    except Exception:  # noqa: BLE001 -- no worker in the room (or LiveKit down): the run still finishes
        put_metric("huddle.frame.no_worker")
        logger.info("voice frames not relayed room=%s", room_id, exc_info=True)
        # 202, not 200: accepted but nobody heard it. The bridge posts a completed run's answer
        # into the room itself when its closing batch comes back undelivered (the call ended).
        return JSONResponse({"ok": True, "delivered": False}, status_code=202)
    return {"ok": True, "delivered": True}
