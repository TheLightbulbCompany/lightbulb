"""Recall's per-person audio socket for one meeting occurrence. Recall dials OUT to us (through the
raw-WS door, ``/api/v1/gateway/*``) with the audio ticket ``meeting_bots.create`` minted, and
reconnects with the same one; it dies with the booking's nonce or its expiry. Each socket gets its
own ``MeetBridge``: a reconnect landing on another task seats the same identities, and LiveKit
drops the older duplicates. Keepalive is uvicorn's protocol ping (20 s), which Recall needs
under 30 s through the proxies.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket

from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import livekit_huddles, meet_bridge

logger = logging.getLogger(__name__)
router = APIRouter()
_LIVE = ("bot_created", "in_call")


async def _authorized(room: str, ticket: str) -> bool:
    found = livekit_huddles.verify_meet_ticket(ticket, livekit_huddles.AUDIO_TICKET)
    if not found or found[0] != room:
        return False
    meeting = ((await messaging_repo.get_room(room)) or {}).get("meeting") or {}
    return (
        bool(meeting.get("recallBotId"))
        and meeting.get("ticketNonce") == found[1]
        and meeting.get("status") in _LIVE
        # never beside a page that publishes the mixed stream: the worker would hear everyone twice
        and meeting.get("audio") == "separate"
    )


@router.websocket("/meet")
@router.websocket("/meet/")
async def meet_audio_ws(websocket: WebSocket) -> None:
    room = websocket.query_params.get("room", "")
    try:
        ok = await _authorized(room, websocket.query_params.get("token", ""))
    except Exception:  # noqa: BLE001 -- a DynamoDB blip: Recall retries the connect
        put_metric("meeting.bridge.error", dimensions={"why": "auth"})
        logger.warning("meet audio auth failed room=%s", room, exc_info=True)
        await websocket.close(code=1011)
        return
    if not ok:
        await websocket.close(code=1008, reason="Unauthorized")
        return
    await websocket.accept()
    bridge = meet_bridge.MeetBridge(room)
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            frame = msg.get("text") or (msg.get("bytes") or b"").decode("utf-8", "replace")
            await bridge.feed(frame)
    except Exception:  # noqa: BLE001 -- the socket died; the bridge is closed below either way
        logger.info("meet audio socket ended room=%s", room, exc_info=True)
    finally:
        await bridge.close()
