"""The forwarding check (onboarding "Keep my number"): one outbound call from the assistant's line to the owner's
business line. With forwarding set, the carrier sends it straight back to the line, and the inbound start
(routers/voice_phone.start_call) sees our own probe, marks `forwardingOk` and hangs both legs up.

LiveKit's documented outbound path: CreateSIPParticipant on a stored outbound trunk
(https://docs.livekit.io/sip/outbound-calls/, https://docs.livekit.io/reference/telephony/sip-api/#createsipparticipant).
No agent is dispatched into the `phone-out-` room; the leg is capped by LiveKit itself (ringing_timeout and
max_call_duration), so nothing here runs a timer. DeleteRoom ends it early on detection.
"""

from __future__ import annotations

import logging
import uuid

from google.protobuf.duration_pb2 import Duration
from livekit import api

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import livekit_huddles

logger = logging.getLogger(__name__)

PROBE_S = 25  # the outbound leg rings and lives at most this long
WINDOW_S = 30  # how long after the dial an arriving call counts as the probe


async def forward_check(ws: str, cfg: dict) -> dict:
    from routers.voice import _e164  # the page's number parser; routers.voice imports this module

    if not (settings.VOICE_OUTBOUND_TRUNK_ID and livekit_huddles.configured()):
        return {"ok": False, "reason": "outbound_not_configured"}
    to = _e164(str(cfg.get("forwardFrom") or ""))
    line = str(cfg.get("number") or "") or settings.VOICE_LINE_NUMBER
    if not to:
        return {"ok": False, "reason": "no_business_line"}
    if not line or ((await messaging_repo._get(f"PHONE#{line}", "LINE")) or {}).get("ownerId") != ws:
        return {"ok": False, "reason": "no_line"}  # the call coming back must reach this workspace's line
    call_id = uuid.uuid4().hex
    # Written before the dial so the call coming back always finds it; a new check clears the old tick.
    rest = {k: v for k, v in cfg.items() if k not in ("forwardingOk", "forwardingOkAt")}
    await messaging_repo.put_voice_agent(
        ws, {**rest, "forwardingCheck": {"startedAt": utc_now_iso(), "callId": call_id}}, "voice"
    )
    try:
        async with livekit_huddles._api() as lk:
            await lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                sip_trunk_id=settings.VOICE_OUTBOUND_TRUNK_ID, sip_call_to=to, sip_number=line,
                room_name=f"phone-out-{call_id}", participant_identity="forward-check",
                ringing_timeout=Duration(seconds=PROBE_S), max_call_duration=Duration(seconds=PROBE_S),
            ))  # fmt: skip
    except Exception:  # noqa: BLE001 -- Twirp, aiohttp and timeouts alike
        logger.warning("forward check dial failed ws=%s", ws, exc_info=True)
        return {"ok": False, "reason": "dial_failed"}
    return {"ok": True}


async def end_probe(inbound_room: str, call_id: str) -> None:
    """Hang up both legs of a detected probe: the inbound room the carrier forwarded into, and our outbound one."""
    for room in (inbound_room, f"phone-out-{call_id}"):
        await livekit_huddles._best_effort(
            "end_probe", room, lambda lk, r=room: lk.room.delete_room(api.DeleteRoomRequest(room=r))
        )
