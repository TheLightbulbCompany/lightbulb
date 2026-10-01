"""LiveKit adapter for huddles. Every network call goes through ``_api()`` so tests fake one seam."""

from __future__ import annotations

import json
from datetime import timedelta

from livekit import api
from livekit.protocol import models as lk_models

from core.config import settings

AGENT_IDENTITY = "agent-ceo"
AGENT_NAME = "lightbulb-huddle"
FRAME_TOPIC = "call.frame"


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


async def send_frames(room_id: str, events: list[dict]) -> None:
    async with _api() as lk:
        await lk.room.send_data(
            api.SendDataRequest(
                room=room_name(room_id),
                data=json.dumps({"events": events}).encode(),
                destination_identities=[AGENT_IDENTITY],
                topic=FRAME_TOPIC,
                kind=lk_models.DataPacket.RELIABLE,
            )
        )
