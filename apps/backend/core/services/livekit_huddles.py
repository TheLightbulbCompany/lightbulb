"""LiveKit adapter for huddles. Every network call goes through ``_api()`` so tests fake one seam."""

from __future__ import annotations

import json
from datetime import timedelta

from livekit import api
from livekit.protocol import models as lk_models

from core.config import settings
from core.observability.metrics import put_metric

AGENT_IDENTITY = "agent-ceo"
AGENT_NAME = "lightbulb-huddle"
FRAME_TOPIC = "call.frame"
MAX_PACKET_BYTES = 14_000  # LiveKit drops data packets over ~15 KB
_OVERHEAD = b'{"events":[]}'


def configured() -> bool:
    return bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET)


def room_name(room_id: str) -> str:
    return f"huddle-{settings.ENVIRONMENT}-{room_id}"


def _api() -> api.LiveKitAPI:
    return api.LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)


def mint_token(*, room_id: str, user_id: str, name: str) -> str:
    return (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(user_id)
        .with_name(name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name(room_id),
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(timedelta(minutes=15))
        .to_jwt()
    )


async def _identities(room_id: str) -> list[str]:
    async with _api() as lk:
        try:
            res = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name(room_id)))
        except api.TwirpError as e:
            if e.code == api.TwirpErrorCode.NOT_FOUND:
                return []
            raise
    return [p.identity for p in res.participants]


async def has_ceo(room_id: str) -> bool:
    return AGENT_IDENTITY in await _identities(room_id)


async def participants(room_id: str) -> list[str]:
    return [i for i in await _identities(room_id) if i != AGENT_IDENTITY]


async def dispatch_ceo(room_id: str, metadata: dict) -> None:
    async with _api() as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME, room=room_name(room_id), metadata=json.dumps(metadata)
            )
        )


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
    async with _api() as lk:
        for data in packets:  # in order: the worker's reorder window keys on frame seq, not arrival
            await lk.room.send_data(
                api.SendDataRequest(
                    room=room_name(room_id),
                    data=data,
                    destination_identities=[AGENT_IDENTITY],
                    topic=FRAME_TOPIC,
                    kind=lk_models.DataPacket.RELIABLE,
                )
            )
