"""Recall drives a meeting room's lifecycle: the bot's status webhook opens and closes the LiveKit
room and dispatches the worker; the realtime endpoint relays who is speaking; the worker's idle
leave pulls the bot. Both Recall routes are signed with the workspace verification secret, checked
before the body is parsed. The status route works inline (Recall retries it only on a non-2xx);
the realtime route answers 2xx at once -- Recall delivers those serially.
"""

import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import clerk_admin, livekit_huddles, meeting_bots, meeting_invites, messaging, voice_calls
from core.services.live_events import get_live_event_publisher
from routers.huddles import RoomBody, _problem, _require_room_token, _room_id, _Worker

logger = logging.getLogger(__name__)
router = APIRouter()

_TARGET = {
    "in_call_recording": "in_call",
    "in_call_not_recording": "in_call",
    "call_ended": "ended",
    "done": "ended",
    "fatal": "bot_failed",
}
_TERMINAL = {"ended", "bot_failed", "cancelled"}
_FATAL_TEXT = {"google_meet_knocking_disabled": "The host didn't let the agents in."}
_SPEAKING = {"speech_on": True, "speech_off": False, "join": False, "leave": False}
_notes_missing_logged = False


async def _verified(request: Request) -> dict | None:
    """The parsed body of a signed request (401 otherwise); None when meetings are off."""
    if not settings.MEETINGS_ENABLED:  # before the signature: a 401 is retried for 24 h
        logger.info("recall webhook: MEETINGS_ENABLED off, ignored")
        return None
    body = await request.body()
    if not meeting_bots.verify_webhook(request.headers, body):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _bot(payload: dict | None) -> tuple[str, str, dict] | None:
    """``(bot_id, room_id, data.data)`` from a payload, or None for a bot that is not one of ours."""
    data = (payload or {}).get("data") or {}
    bot = data.get("bot") or {}
    try:
        room_id = messaging.validate_uuid(str((bot.get("metadata") or {}).get("roomId") or ""), label="room ID")
        return str(bot["id"]), room_id, data.get("data") or {}
    except Exception:  # noqa: BLE001 -- KeyError or a malformed id alike: not ours
        return None


async def _publish(room_id: str, status: str) -> None:
    try:
        await get_live_event_publisher().publish_room(
            room_id,
            messaging.make_live_event("room.meeting.changed", {"roomId": room_id, "status": status}, room_id=room_id),
        )
    except Exception:  # noqa: BLE001 -- live delivery is a hint; clients re-read the room
        logger.warning("meeting room %s: live publish failed", room_id, exc_info=True)


def _notes(room_id: str) -> None:
    global _notes_missing_logged
    try:
        from core.services import meeting_notes  # Task 12
    except ImportError:
        if not _notes_missing_logged:
            _notes_missing_logged = True
            logger.info("meeting_notes not built yet: no notes run")
        return
    voice_calls._fire(meeting_notes.on_ended(room_id))


def _claim_name(room_id: str, target: str) -> str:
    # Every terminal path (call_ended, done, fatal, the idle leave) shares one claim.
    return f"meet-{'in_call' if target == 'in_call' else 'ended'}:{room_id}"


async def _finish(room_id: str, status: str, sub_code: str, notes: bool) -> None:
    """The rest of the one end path, once ``status`` is written: the LiveKit room closed, the
    metric, the live hint, the notes run."""
    await livekit_huddles.close_room(room_id, meet=True)
    put_metric("meeting.ended", dimensions={"sub_code": sub_code or "none"})
    await _publish(room_id, status)
    if notes:
        _notes(room_id)


async def _on_status(bot_id: str, room_id: str, code: str, sub_code: str) -> None:
    target = _TARGET[code]
    meta = await messaging_repo.get_room(room_id) or {}
    meeting = meta.get("meeting") or {}
    status = meeting.get("status")
    if (
        meeting.get("recallBotId") != bot_id
        or status in _TERMINAL
        or target == status
        or code == meeting.get("lastBotStatus")
    ):
        return  # another bot's, a replay, or a step this meeting already took
    # Svix delivers in parallel and Meet sends in_call_not_recording then in_call_recording within
    # a second: one transition per room and kind, or the second dispatch evicts the first worker.
    claim = _claim_name(room_id, target)
    if not await container_repo.try_claim_pass(claim, interval_s=60):
        return
    owner_id = meta["ownerId"]
    try:
        if target == "in_call":
            await meeting_invites._patch_meeting(room_id, {"status": "in_call", "lastBotStatus": code})
            try:
                org = await clerk_admin.get_organization(owner_id) or {}
            except Exception:  # noqa: BLE001 -- a Clerk blip must not cost the meeting its agents
                org = {}
            metadata = {
                "roomId": room_id,
                "ownerId": owner_id,
                "roomToken": livekit_huddles.room_token(room_id, owner_id),
                "source": "meet",
                "company": org.get("name") or "Your team",
            }
            try:
                await livekit_huddles.dispatch_meet(room_id, metadata)
            except Exception:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike
                put_metric("huddle.livekit.error")
                logger.warning("meeting room %s: worker dispatch failed", room_id, exc_info=True)
            voice_calls.prewake_owner(owner_id, team=await voice_calls.room_agents(room_id))
            await _publish(room_id, "in_call")
        elif target == "ended":
            await meeting_invites._patch_meeting(room_id, {"status": "ended", "lastBotStatus": code})
            await _finish(room_id, "ended", sub_code, notes=True)
        else:
            text = _FATAL_TEXT.get(sub_code) or f"The agents couldn't join this meeting ({sub_code or 'unknown'})."
            await meeting_invites._post_line(owner_id, room_id, f"meeting:{bot_id}:fatal", text)
            fields = {"status": "bot_failed", "botError": sub_code, "lastBotStatus": code}
            await meeting_invites._patch_meeting(room_id, fields)
            # A bot that dies mid-call still leaves a conversation worth its notes.
            await _finish(room_id, "bot_failed", sub_code, notes=status == "in_call")
    except Exception:
        await container_repo.release_pass(claim)  # Recall's retry must find the door open
        raise


@router.post("/webhooks/recall")
async def recall_status(request: Request):
    """Inline, not backgrounded: Recall retries a status webhook for 24 h only on a non-2xx, so a
    failure must answer 5xx (the work fits Recall's 15 s budget)."""
    payload = await _verified(request)
    found = _bot(payload)
    if found:
        bot_id, room_id, data = found
        code = str(data.get("code") or payload.get("event") or "").removeprefix("bot.")
        if code in _TARGET:
            try:
                await _on_status(bot_id, room_id, code, str(data.get("sub_code") or ""))
            except Exception:
                put_metric("meeting.webhook.error")
                logger.warning("recall status %s failed room=%s", code, room_id, exc_info=True)
                raise HTTPException(status_code=500, detail="retry") from None
    return {"ok": True}


def _at(stamp: dict | None) -> float:
    try:
        return datetime.fromisoformat(str((stamp or {})["absolute"]).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001 -- a missing or odd timestamp: arrival time is close enough
        return time.time()


async def _speaker(bot_id: str, room_id: str, event: dict) -> None:
    try:
        meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if meeting.get("recallBotId") != bot_id:
            return  # a stale or rebooked bot pushes nothing into the room
        await livekit_huddles.send_meet_speaker(room_id, event)
    except Exception:  # noqa: BLE001 -- already answered 2xx; the next event carries on
        put_metric("meeting.webhook.error")
        logger.warning("recall realtime failed room=%s", room_id, exc_info=True)


@router.post("/webhooks/recall/realtime")
async def recall_realtime(request: Request, tasks: BackgroundTasks):
    payload = await _verified(request)
    found = _bot(payload)
    kind = str((payload or {}).get("event") or "").removeprefix("participant_events.")
    if found and kind in _SPEAKING:
        bot_id, room_id, data = found
        who = data.get("participant") or {}
        event = {
            "participantId": str(who.get("id") or ""),
            "name": str(who.get("name") or ""),
            "speaking": _SPEAKING[kind],
            "at": _at(data.get("timestamp")),
        }
        tasks.add_task(_speaker, bot_id, room_id, event)
    return {"ok": True}


@router.delete("/internal/meetings/{room_id}", dependencies=[_Worker])
async def leave(room_id: str, body: RoomBody):
    """The worker's idle leave: end the meeting, then pull the bot (its call_ended no-ops)."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != body.ownerId:
        raise _problem(404, "room_not_found")
    meeting = meta.get("meeting") or {}
    status = meeting.get("status")
    if not meeting.get("recallBotId") or status in _TERMINAL:
        return {"ok": True}
    claim = _claim_name(room_id, "ended")
    if not await container_repo.try_claim_pass(claim, interval_s=60):
        return {"ok": True}  # a call_ended is already ending it
    # Ended BEFORE the leave, so the call_ended the leave provokes finds the step taken.
    await meeting_invites._patch_meeting(room_id, {"status": "ended", "lastBotStatus": "idle_leave"})
    try:
        await meeting_bots.leave(meeting["recallBotId"])
    except Exception:
        # The bot is still in the call: undo, so the worker's retry (or Recall's end) can end it.
        await meeting_invites._patch_meeting(room_id, {"status": status, "lastBotStatus": meeting.get("lastBotStatus")})
        await container_repo.release_pass(claim)
        raise
    await _finish(room_id, "ended", "idle_leave", notes=True)
    return {"ok": True}
