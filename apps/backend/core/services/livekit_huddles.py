"""LiveKit adapter for huddles. Every network call goes through ``_api()`` so tests fake one seam."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

from livekit import api
from livekit.protocol import models as lk_models

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo

logger = logging.getLogger(__name__)
AGENT_IDENTITY = "agent-ceo"
AGENT_NAME = "lightbulb-huddle"
FRAME_TOPIC = "call.frame"
MEET_SPEAKER_TOPIC = "meet.speaker"
MAX_PACKET_BYTES = 14_000  # LiveKit drops data packets over ~15 KB
_OVERHEAD = b'{"events":[]}'


def configured() -> bool:
    return bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET)


def room_token(room_id: str, owner_id: str) -> str:
    """Binds a dispatched worker to its room: the internal routes accept only this room's token."""
    key = settings.HUDDLE_WORKER_SECRET.encode()
    return hmac.new(key, f"{room_id}:{owner_id}".encode(), hashlib.sha256).hexdigest()


def meet_ticket(room_id: str, nonce: str, exp_epoch: int) -> str:
    """The Recall bot page's credential: ``base64url(room|nonce|exp).hmac``. The nonce (stored on
    the room META as ``meeting.ticketNonce``) binds it to one booked bot, since Recall assigns
    the bot id only after the page URL is already fixed. Task 8 verifies it."""
    if not settings.LIVEKIT_API_SECRET:
        raise RuntimeError("LIVEKIT_API_SECRET unset: no meet ticket")
    body = base64.urlsafe_b64encode(f"{room_id}|{nonce}|{exp_epoch}".encode()).decode().rstrip("=")
    return f"{body}.{hmac.new(settings.LIVEKIT_API_SECRET.encode(), b'meet-ticket|' + body.encode(), hashlib.sha256).hexdigest()}"


def verify_meet_ticket(ticket: str) -> tuple[str, str] | None:
    """``(room_id, nonce)`` from a ``meet_ticket`` whose signature holds and whose ``exp`` is ahead."""
    body, _, sig = (ticket or "").partition(".")
    secret = settings.LIVEKIT_API_SECRET
    if not (secret and body and sig):
        return None
    expected = hmac.new(secret.encode(), b"meet-ticket|" + body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        return None
    try:
        room_id, nonce, exp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")
        if int(exp) > time.time():
            return room_id, nonce
    except ValueError:
        pass
    return None


def room_name(room_id: str) -> str:
    return f"huddle-{settings.ENVIRONMENT}-{room_id}"


def meet_room_name(room_id: str) -> str:
    """A meeting's LiveKit room: the Recall bot's page and the worker meet here."""
    return f"meet-{settings.ENVIRONMENT}-{room_id}"


async def live_room_name(room_id: str) -> str:
    """Where the room's voice worker is now: the meeting's room while a meeting is in call, else
    the Live huddle's."""
    meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
    return meet_room_name(room_id) if meeting.get("status") == "in_call" else room_name(room_id)


def _api() -> api.LiveKitAPI:
    return api.LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)


def _token(identity: str, name: str, room: str, ttl: timedelta, sources: list[str] | None = None) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_publish_sources=sources,
    )
    return (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(ttl)
        .to_jwt()
    )


def mint_token(*, room_id: str, user_id: str, name: str) -> str:
    return _token(user_id, name, room_name(room_id), timedelta(minutes=15))


def mint_meet_token(room_id: str) -> str:
    """The Recall bot page's LiveKit seat: its mic (the meeting's mixed audio) and data, for 6 h."""
    return _token(f"meet:{room_id}", "Meeting", meet_room_name(room_id), timedelta(hours=6), ["microphone"])


async def _identities(room: str) -> list[str]:
    async with _api() as lk:
        try:
            res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        except api.TwirpError as e:
            if e.code == api.TwirpErrorCode.NOT_FOUND:
                return []
            raise
    return [p.identity for p in res.participants]


async def has_ceo(room_id: str, *, live: bool = False) -> bool:
    """Is the room's voice worker up -- wherever it is now, or (``live``) in the Live huddle."""
    return AGENT_IDENTITY in await _identities(room_name(room_id) if live else await live_room_name(room_id))


async def participants(room_id: str) -> list[str]:
    """The people in the room's Live huddle."""
    return [i for i in await _identities(room_name(room_id)) if i != AGENT_IDENTITY]


async def _dispatch(room: str, metadata: dict) -> None:
    async with _api() as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room, metadata=json.dumps(metadata))
        )


async def dispatch_ceo(room_id: str, metadata: dict) -> None:
    await _dispatch(room_name(room_id), metadata)


async def dispatch_meet(room_id: str, metadata: dict) -> None:
    await _dispatch(meet_room_name(room_id), metadata)


async def _best_effort(what: str, room_id: str, call) -> None:
    """Revocation side effects: never raise into the caller (the membership change already committed)."""
    if not configured():
        return
    try:
        async with _api() as lk:
            await call(lk)
    except Exception as e:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike
        if isinstance(e, api.TwirpError) and e.code == api.TwirpErrorCode.NOT_FOUND:
            return  # no huddle running, or they already left
        put_metric("huddle.livekit.error")
        logger.warning("huddle %s failed room=%s", what, room_id, exc_info=True)


async def remove_participant(room_id: str, identity: str) -> None:
    """A removed member must stop hearing the huddle: LiveKit refreshes a connected participant's token."""
    await _best_effort(
        "remove_participant",
        room_id,
        lambda lk: lk.room.remove_participant(api.RoomParticipantIdentity(room=room_name(room_id), identity=identity)),
    )


async def close_room(room_id: str, *, meet: bool = False) -> None:
    """An archived or deleted channel's huddle (or an ended meeting's room) ends for everyone (the
    worker's shutdown closes its call)."""
    room = meet_room_name(room_id) if meet else room_name(room_id)
    await _best_effort("close_room", room_id, lambda lk: lk.room.delete_room(api.DeleteRoomRequest(room=room)))


def _packets(events: list[dict]) -> list[bytes]:
    """Greedy-pack events into `{"events":[...]}` JSON packets of at most MAX_PACKET_BYTES each."""
    packets: list[list[bytes]] = []
    size = 0
    for ev in events:
        b = json.dumps(ev, separators=(",", ":")).encode()
        if len(_OVERHEAD) + len(b) > MAX_PACKET_BYTES:
            # Only full-text resync chunks get this big, and the live deltas already carried the text.
            put_metric("huddle.frame.oversize")
            continue
        if packets and size + 1 + len(b) <= MAX_PACKET_BYTES:
            packets[-1].append(b)
            size += 1 + len(b)
        else:
            packets.append([b])
            size = len(_OVERHEAD) + len(b)
    return [b'{"events":[' + b",".join(p) + b"]}" for p in packets]


async def send_frames(room_id: str, events: list[dict]) -> None:
    packets = _packets(events)
    if not packets:
        return
    room = await live_room_name(room_id)
    async with _api() as lk:
        for data in packets:  # in order: the worker's reorder window keys on frame seq, not arrival
            await lk.room.send_data(
                api.SendDataRequest(
                    room=room,
                    data=data,
                    destination_identities=[AGENT_IDENTITY],
                    topic=FRAME_TOPIC,
                    kind=lk_models.DataPacket.RELIABLE,
                )
            )


async def send_meet_speaker(room_id: str, event: dict) -> None:
    """One Recall speaker event to everyone in the meeting's room (the worker reads who is talking)."""
    # ponytail: a LiveKitAPI per event, like every other call here (no long-lived client exists in
    # this module); keep one per process, closed on shutdown, if speaker events show in latency.
    async with _api() as lk:
        await lk.room.send_data(
            api.SendDataRequest(
                room=meet_room_name(room_id),
                data=json.dumps(event, separators=(",", ":")).encode(),
                topic=MEET_SPEAKER_TOPIC,
                kind=lk_models.DataPacket.RELIABLE,
            )
        )
