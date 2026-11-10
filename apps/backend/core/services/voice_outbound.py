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
from datetime import datetime, timezone

from google.protobuf.duration_pb2 import Duration
from livekit import api

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import livekit_huddles

logger = logging.getLogger(__name__)

PROBE_S = 60  # the outbound leg rings and lives at most this long ("missed" forwarding waits ~20-30 s; LiveKit max 80)
WINDOW_S = 70  # how long after the dial an arriving call counts as the probe


async def forward_check(ws: str, cfg: dict) -> dict:
    from routers.voice import _e164, us_number  # the page's number parsers; routers.voice imports this module

    if not (settings.VOICE_OUTBOUND_TRUNK_ID and livekit_huddles.configured()):
        return {"ok": False, "reason": "outbound_not_configured"}
    raw = str(cfg.get("forwardFrom") or "")
    to = us_number(raw)
    line = str(cfg.get("number") or "") or settings.VOICE_LINE_NUMBER
    if not _e164(raw):
        return {"ok": False, "reason": "no_business_line"}
    if not to:
        return {"ok": False, "reason": "us_only"}  # US/CA only: never dial abroad, the Caribbean or a premium line
    if not line or ((await messaging_repo._get(f"PHONE#{line}", "LINE")) or {}).get("ownerId") != ws:
        return {"ok": False, "reason": "no_line"}  # the call coming back must reach this workspace's line
    call_id = uuid.uuid4().hex
    # One dial per window per workspace: the lock is a conditional write, so two POSTs in the same instant dial once.
    if age_s(cfg.get("forwardingCheck")) <= WINDOW_S or not await messaging_repo.claim_voice_slot(
        ws, "VOICEFWDCHECK#lock", call_id, WINDOW_S
    ):
        return {"ok": False, "reason": "check_in_progress"}
    # Written before the dial so the call coming back always finds it. A failed dial leaves it (and the lock), so a
    # retry waits out the window too.
    await messaging_repo.put_voice_agent(
        ws, {**cfg, "forwardingCheck": {"startedAt": utc_now_iso(), "callId": call_id}}, "voice"
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
    # The dial started: only now does the old tick go (unless this check already came back and set a new one).
    now = await messaging_repo.get_voice_agent(ws) or {}
    if (now.get("forwardingCheck") or {}).get("callId") == call_id:
        for k in ("forwardingOk", "forwardingOkAt"):
            now.pop(k, None)
        await messaging_repo.put_voice_agent(ws, now, "voice")
    return {"ok": True}


def age_s(check) -> float:
    """Seconds since a forwardingCheck started; infinite when there is none (or it is unreadable)."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(check["startedAt"])).total_seconds()
    except (TypeError, KeyError, ValueError):
        return float("inf")


async def end_probe(inbound_room: str, call_id: str) -> None:
    """Hang up both legs of a detected probe: the inbound room the carrier forwarded into, and our outbound one."""
    for room in (inbound_room, f"phone-out-{call_id}"):
        await livekit_huddles._best_effort(
            "end_probe", room, lambda lk, r=room: lk.room.delete_room(api.DeleteRoomRequest(room=r))
        )
