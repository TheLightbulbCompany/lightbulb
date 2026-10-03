"""Recall drives a meeting room's lifecycle: the bot's status webhook opens and closes the LiveKit
room and dispatches the worker; the realtime endpoint relays who is speaking; the worker's idle
leave pulls the bot. Both Recall routes are signed with the workspace verification secret, checked
before the body is parsed, and answer 2xx at once -- Recall delivers realtime events serially.
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
    body = await request.body()
    if not meeting_bots.verify_webhook(request.headers, body):
        raise HTTPException(status_code=401, detail="invalid signature")
    if not settings.MEETINGS_ENABLED:
        logger.info("recall webhook: MEETINGS_ENABLED off, ignored")
        return None
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


async def _end(room_id: str, fields: dict, sub_code: str) -> None:
    """The one end path: status, the LiveKit room closed, the metric, the notes run."""
    await meeting_invites._patch_meeting(room_id, fields)
    await livekit_huddles.close_room(room_id, meet=True)
    put_metric("meeting.ended", dimensions={"sub_code": sub_code or "none"})
    await _publish(room_id, fields["status"])
    if fields["status"] == "ended":
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
    # a second: one transition per room and target, or the second dispatch evicts the first worker.
    if not await container_repo.try_claim_pass(f"meet-{target}:{room_id}", interval_s=60):
        return
    owner_id = meta["ownerId"]
    if target == "in_call":
        await meeting_invites._patch_meeting(room_id, {"status": "in_call", "lastBotStatus": code})
        org = await clerk_admin.get_organization(owner_id) or {}
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
        voice_calls.prewake_owner(owner_id)
        await _publish(room_id, "in_call")
    elif target == "ended":
        await _end(room_id, {"status": "ended", "lastBotStatus": code}, sub_code)
    else:
        await _end(room_id, {"status": "bot_failed", "botError": sub_code, "lastBotStatus": code}, sub_code)
        text = _FATAL_TEXT.get(sub_code) or f"The agents couldn't join this meeting ({sub_code or 'unknown'})."
        await meeting_invites._post_line(owner_id, room_id, f"meeting:{bot_id}:fatal", text)


async def _background(what: str, coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 -- we already answered 2xx; Recall will not retry
        put_metric("meeting.webhook.error")
        logger.warning("recall %s handling failed", what, exc_info=True)


@router.post("/webhooks/recall")
async def recall_status(request: Request, tasks: BackgroundTasks):
    payload = await _verified(request)
    found = _bot(payload)
    if found:
        bot_id, room_id, data = found
        code = str(data.get("code") or payload.get("event") or "").removeprefix("bot.")
        if code in _TARGET:
            tasks.add_task(_background, "status", _on_status(bot_id, room_id, code, str(data.get("sub_code") or "")))
    return {"ok": True}


def _at(stamp: dict | None) -> float:
    try:
        return datetime.fromisoformat(str((stamp or {})["absolute"]).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001 -- a missing or odd timestamp: arrival time is close enough
        return time.time()


@router.post("/webhooks/recall/realtime")
async def recall_realtime(request: Request, tasks: BackgroundTasks):
    payload = await _verified(request)
    found = _bot(payload)
    kind = str((payload or {}).get("event") or "").removeprefix("participant_events.")
    if found and kind in _SPEAKING:
        _, room_id, data = found
        who = data.get("participant") or {}
        event = {
            "participantId": str(who.get("id") or ""),
            "name": str(who.get("name") or ""),
            "speaking": _SPEAKING[kind],
            "at": _at(data.get("timestamp")),
        }
        tasks.add_task(_background, "realtime", livekit_huddles.send_meet_speaker(room_id, event))
    return {"ok": True}


@router.delete("/internal/meetings/{room_id}", dependencies=[_Worker])
async def leave(room_id: str, body: RoomBody):
    """The worker's idle leave: pull the bot and end the meeting (the later ``call_ended`` no-ops)."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != body.ownerId:
        raise _problem(404, "room_not_found")
    meeting = meta.get("meeting") or {}
    if meeting.get("recallBotId") and meeting.get("status") not in _TERMINAL:
        await meeting_bots.leave(meeting["recallBotId"])
        await _end(room_id, {"status": "ended"}, "idle_leave")
    return {"ok": True}
