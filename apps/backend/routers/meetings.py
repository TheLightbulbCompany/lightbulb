"""Recall drives a meeting room's lifecycle: the bot's status webhook opens and closes the LiveKit
room and dispatches the worker; the realtime endpoint relays who is speaking; the worker's idle
leave pulls the bot. Both Recall routes are signed with the workspace verification secret, checked
before the body is parsed. The status route works inline (Recall retries it only on a non-2xx);
the realtime route answers 2xx at once -- Recall delivers those serially.
"""

import asyncio
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import (
    clerk_admin,
    livekit_huddles,
    meeting_bots,
    meeting_invites,
    meeting_notes,
    messaging,
    voice_calls,
)
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
_LEAVE_TRIES, _LEAVE_GAP_S = 3, 2
_SPEAKING = {"speech_on": True, "speech_off": False, "join": False, "leave": False}


async def _verified(request: Request) -> dict | None:
    """The parsed body of a signed request (401 otherwise); None when meetings are unavailable."""
    if not settings.meetings_available():  # before the signature: a 401 is retried for 24 h
        logger.info("recall webhook: meetings unavailable here, ignored")
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
    env = (bot.get("metadata") or {}).get("env")
    if env and env != settings.ENVIRONMENT:
        return None  # Recall's webhook is account-wide: another environment's bot is not ours to touch
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


async def _finish(room_id: str, status: str, sub_code: str, notes: bool, in_call_at: int | None = None) -> None:
    """The rest of the one end path, once ``status`` is written. Never raises: the written status
    turns every retry into a no-op, so a step that failed here would otherwise be lost for good.
    Notes first (only for a call the bot was in; a series advances after them, or at once without
    them), the room's close last. Presence is a metric only: we pay for the bot's time, the
    workspace's allowance pays only for agent speech (voice_billing). Both are fired, never awaited:
    the caller may hold the meeting lease that ``advance`` waits for."""
    presence = time.time() - in_call_at if in_call_at else None
    notes = notes and bool(in_call_at)
    steps = [
        ("presence", lambda: put_metric("meeting.presence.seconds", presence, unit="Seconds") if presence else None),
        (
            "notes",
            lambda: voice_calls._fire(meeting_notes.on_ended(room_id) if notes else meeting_invites.advance(room_id)),
        ),
        ("metric", lambda: put_metric("meeting.ended", dimensions={"sub_code": sub_code or "none"})),
        ("publish", lambda: _publish(room_id, status)),
        ("close_room", lambda: livekit_huddles.close_room(room_id, meet=True)),
    ]
    for step, run in steps:
        try:
            result = run()
            if result is not None:
                await result
        except Exception:  # noqa: BLE001 -- one failed step must not cost the others
            put_metric("meeting.finish_step_failed", dimensions={"step": step})
            logger.warning("meeting room %s: end step %s failed", room_id, step, exc_info=True)


async def _on_status(
    bot_id: str, room_id: str, code: str, sub_code: str, nonce: str = "", at: float | None = None
) -> None:
    """One transition, under the meeting's lease and decided from a META read taken inside it, so
    an invite, another status (Meet sends two in-call codes within a second) or the idle leave
    cannot interleave. A busy lease raises: the route answers 5xx and Recall redelivers."""
    target = _TARGET[code]
    async with meeting_invites.meeting_lease(room_id):
        meta = await messaging_repo.get_room(room_id)
        if not meta:
            return  # a room this environment lacks: another environment's (untagged) bot, never ejected
        meeting = meta.get("meeting") or {}
        status = meeting.get("status")
        if meeting.get("recallBotId") != bot_id:
            # Recall books a second bot on a repeated create: the one of this booking (its nonce)
            # that the room lost track of is adopted; any other live bot is told to leave.
            if (
                (not meeting.get("recallBotId") or status == "bot_failed")
                and status not in ("cancelled", "ended")
                and nonce
                and nonce == meeting.get("ticketNonce")
            ):
                audio = await meeting_invites.adopted_audio(room_id, bot_id)
                fields = {"recallBotId": bot_id, "status": "bot_created", **({"audio": audio} if audio else {})}
                drop = ("botError", "lastBotStatus", *(() if audio else ("audio",)))
                meeting = await meeting_invites._patch_meeting(room_id, fields, drop=drop)
                status = "bot_created"
            else:
                if target == "in_call":
                    try:
                        await meeting_bots.leave(bot_id)
                    except Exception:  # noqa: BLE001 -- automatic_leave reaps it anyway; 200 either way
                        logger.warning("meeting room %s: extra bot leave failed", room_id, exc_info=True)
                return
        if status in _TERMINAL:  # never revived; a redelivery retries notes or a series' advance left undone
            if meeting_notes.owed(meeting):
                voice_calls._fire(meeting_notes.on_ended(room_id))
            elif meeting.get("recurrence"):
                voice_calls._fire(meeting_invites.advance(room_id))
            return
        if target == status or code == meeting.get("lastBotStatus"):
            return  # a replay, or a step this meeting already took
        owner_id = meta["ownerId"]
        if target == "in_call":
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
            # Dispatch BEFORE in_call is written: a failure raises with the step untaken, so
            # Recall's retry dispatches again instead of leaving a paid bot with no worker.
            try:
                await livekit_huddles.dispatch_meet(room_id, metadata)
            except Exception:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike
                put_metric("huddle.livekit.error")
                raise
            await meeting_invites._patch_meeting(
                room_id, {"status": "in_call", "lastBotStatus": code, "inCallAt": int(time.time())}
            )
            voice_calls.prewake_owner(owner_id, team=await voice_calls.room_agents(room_id), room_id=room_id)
            await _publish(room_id, "in_call")
        elif target == "ended":
            # endedAt is written once (a terminal status returns above on every later webhook), from
            # the event's own time: a retried or late delivery is not when the call ended.
            fields = {"status": "ended", "lastBotStatus": code, "endedAt": int(at or time.time())}
            await meeting_invites._patch_meeting(room_id, fields)
            await _finish(room_id, "ended", sub_code, notes=True, in_call_at=meeting.get("inCallAt"))
        else:
            title = meeting.get("title") or "Meeting"
            text = (
                f"The host didn't let the agents into **{title}**."
                if sub_code == "google_meet_knocking_disabled"
                else f"The agents couldn't join **{title}** ({sub_code or 'unknown'})."
            )
            await meeting_invites._post_line(owner_id, room_id, f"meeting:{bot_id}:fatal", text)
            fields = {
                "status": "bot_failed",
                "botError": sub_code,
                "lastBotStatus": code,
                "endedAt": int(at or time.time()),
            }
            await meeting_invites._patch_meeting(room_id, fields)
            # A bot that dies mid-call still leaves a conversation worth its notes.
            await _finish(
                room_id, "bot_failed", sub_code, notes=status == "in_call", in_call_at=meeting.get("inCallAt")
            )


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
                nonce = str((payload["data"]["bot"].get("metadata") or {}).get("nonce") or "")
                at = _at({"absolute": data.get("updated_at")})  # arrival time when absent or unreadable
                await _on_status(bot_id, room_id, code, str(data.get("sub_code") or ""), nonce, at)
            except meeting_invites.MeetingBusy:
                raise HTTPException(status_code=503, detail="retry") from None
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


def _rostered(roster: dict | None, kind: str, pid: str, name: str, at: int, host: bool = False) -> dict:
    """The Meet's people after one event: a join adds, a leave removes. The bot is never one.
    Each entry keeps its event time (ms): realtime events run as overlapping background tasks, so
    an older join that commits after a newer leave must not bring the person back."""
    roster = dict(roster or {})
    seen = roster.get(pid) or {}
    if kind not in ("join", "leave") or not pid or at < seen.get("at", 0):
        return roster
    if kind == "leave":
        roster[pid] = {**seen, "at": at, "in": False}
    elif name and not name.endswith(meeting_bots._BOT_SUFFIX):
        # host: Recall's is_host, what the meeting agent checks a member's own speech by (voice_meet)
        roster[pid] = {"name": name[:_ROSTER_NAME_CHARS], "at": at, "in": True, **({"host": True} if host else {})}
    # Bounded: it rides on the room's META item. The oldest leavers go first (enough recent ones
    # stay to refuse a reordered event), then the oldest people present.
    by_age = sorted(roster, key=lambda k: (bool(roster[k].get("in")), roster[k].get("at", 0)))
    for k in by_age[: max(0, len(roster) - _ROSTER_ENTRIES)]:
        del roster[k]
    return roster


_ROSTER_NAMES, _ROSTER_NAME_CHARS, _ROSTER_ENTRIES = 50, 80, 200


def _names(roster: dict) -> list[str]:
    """The names every speaker packet carries, bounded: LiveKit drops a data packet over ~15 KB
    (livekit_huddles.MAX_PACKET_BYTES), and the worker only needs to know when one person is there."""
    names = sorted(p["name"][:_ROSTER_NAME_CHARS] for p in roster.values() if p.get("in") and p.get("name"))
    return names[:_ROSTER_NAMES]


async def _speaker(bot_id: str, room_id: str, event: dict) -> None:
    """Relays one event with the roster. The roster lives on META because Meet sends a join once,
    often before the worker is in the room; every event carries it so a late worker catches up."""
    try:
        meta = (await messaging_repo.get_room(room_id)) or {}
        meeting = meta.get("meeting") or {}
        if meeting.get("recallBotId") != bot_id:
            return  # a stale or rebooked bot pushes nothing into the room
        kind, pid, name, at = event["kind"], event["participantId"], event["name"], int(event["at"] * 1000)
        host = bool(event.get("host"))
        roster = _rostered(meeting.get("roster"), kind, pid, name, at, host)
        if kind in ("join", "leave"):
            try:
                written = await meeting_invites._patch_meeting(
                    room_id,
                    lambda m: {"roster": _rostered(m.get("roster"), kind, pid, name, at, host)},
                    # re-checked on every attempt: an old bot's late join never lands on the next occurrence
                    when=lambda m: m.get("recallBotId") == bot_id,
                )
                if written is None:
                    return  # the occurrence moved on under this event: a stale bot pushes nothing
                roster = written.get("roster", roster)
            except Exception:  # noqa: BLE001 -- best effort: this event still relays its own roster
                put_metric("meeting.roster.write_failed")
                logger.warning("recall realtime room=%s: roster write failed", room_id, exc_info=True)
        await livekit_huddles.send_meet_speaker(room_id, {**event, "roster": _names(roster)})
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
            "kind": kind,
            "participantId": str(who.get("id") or ""),
            "name": str(who.get("name") or ""),
            "speaking": _SPEAKING[kind],  # what a worker before ``kind`` reads
            **({"host": True} if who.get("is_host") else {}),  # Recall's is_host (voice_meet: who may act)
            "at": _at(data.get("timestamp")),
        }
        logger.info("recall realtime room=%s kind=%s participant=%s", room_id, kind, event["participantId"])
        tasks.add_task(_speaker, bot_id, room_id, event)
    return {"ok": True}


@router.delete("/internal/meetings/{room_id}", dependencies=[_Worker])
async def leave(room_id: str, body: RoomBody):
    """The worker's idle leave: end the meeting, then pull the bot (its call_ended no-ops)."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    try:
        async with meeting_invites.meeting_lease(room_id):
            meta = await messaging_repo.get_room(room_id)
            if not meta or meta.get("ownerId") != body.ownerId:
                raise _problem(404, "room_not_found")
            meeting = meta.get("meeting") or {}
            if not meeting.get("recallBotId") or meeting.get("status") in _TERMINAL:
                return {"ok": True}
            # Ended BEFORE the leave, so the call_ended the leave provokes finds the step taken.
            fields = {"status": "ended", "lastBotStatus": "idle_leave", "endedAt": int(time.time())}
            await meeting_invites._patch_meeting(room_id, fields)
    except meeting_invites.MeetingBusy:
        raise _problem(503, "meeting_busy") from None
    for attempt in range(1, _LEAVE_TRIES + 1):
        try:
            await meeting_bots.leave(meeting["recallBotId"])
            break
        except Exception:  # noqa: BLE001 -- never undo: a call_ended may already have seen "ended"
            if attempt < _LEAVE_TRIES:
                await asyncio.sleep(_LEAVE_GAP_S)
                continue
            # Recall's automatic_leave reaps a bot left alone; the LiveKit room still closes below.
            put_metric("meeting.leave_failed")
            logger.warning("meeting room %s: bot leave failed", room_id, exc_info=True)
    await _finish(room_id, "ended", "idle_leave", notes=True, in_call_at=meeting.get("inCallAt"))
    return {"ok": True}
