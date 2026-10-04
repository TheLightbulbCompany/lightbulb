"""Recall.ai bots for meeting rooms. Recall is the scheduler: a bot created with ``join_at`` more
than 10 minutes out is reserved and joins itself, a sooner one joins now; a move is a PATCH, a
cancel a DELETE (or ``leave_call`` once it joined). The room META's ``meeting`` is the only state.
The API key and the meeting URL never reach a log line.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from core.config import settings
from core.services import livekit_huddles

CONSENT_LINE = (
    "Lightbulb agents are in this meeting for {company}. They listen for their names; notes and a "
    "transcript go to {company}'s Lightbulb workspace. Recall.ai relays the call and stores nothing."
)
_REASONS = {
    "402": "the meeting-bot account is out of credit",
    "507": "no meeting bots are free right now",
    "timeout": "the meeting-bot service didn't answer",
    "bad_response": "the meeting-bot service sent an unreadable answer",
}
_BOT_SUFFIX = " agents · Lightbulb"  # Recall caps bot_name at 100 chars; the company part gives way
_TICKET_GRACE_S = 4 * 3600  # the ticket outlives the scheduled end by this much, then dies
_WEBHOOK_TOLERANCE_S = 300
_SPEECH_EVENTS = [
    "participant_events.speech_on",
    "participant_events.speech_off",
    "participant_events.join",
    "participant_events.leave",
]
_AUDIO_EVENTS = ["audio_separate_raw.data", "participant_events.join", "participant_events.leave"]
_ENDED = {"call_ended", "done", "fatal", "recording_done", "media_expired", "analysis_done", "analysis_failed"}
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport


class RecallError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.reason = _REASONS.get(code, f"the meeting-bot service answered {code}")


async def _call(method: str, path: str, body: dict | None = None, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Token {settings.RECALL_API_KEY}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(base_url=settings.RECALL_API_BASE, timeout=15, transport=_transport) as client:
            resp = await client.request(method, path, json=body, headers=headers)
    except httpx.TimeoutException:
        raise RecallError("timeout") from None
    except httpx.HTTPError:
        raise RecallError("network") from None
    if resp.status_code >= 400:
        raise RecallError(str(resp.status_code))
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        raise RecallError("bad_response") from None


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _ticket(room_id: str, meeting: dict, purpose: str) -> str:
    exp = int(meeting.get("endAt") or meeting["startAt"]) + _TICKET_GRACE_S
    return livekit_huddles.meet_ticket(room_id, meeting["ticketNonce"], exp, purpose)


def _output_media(room_id: str, meeting: dict) -> dict:
    ticket = _ticket(room_id, meeting, livekit_huddles.PAGE_TICKET)
    page = settings.MEET_BOT_PAGE_URL or f"{settings.FRONTEND_URL.rstrip('/')}/meet-bot"
    return {"camera": {"kind": "webpage", "config": {"url": f"{page}#t={ticket}"}}}


def audio_url(room_id: str, ticket: str) -> str:
    """Recall dials this socket for the call and pushes each person's audio. It rides the raw-WS
    door (``/api/v1/gateway/*``); Recall 400s a URL with no ``/`` before the ``?``."""
    base = settings.GATEWAY_RELAY_URL.rstrip("/").removesuffix("/ws")
    return f"{base}/meet/?{urlencode({'room': room_id, 't': ticket})}"


def _recording_config(room_id: str, meeting: dict) -> dict:
    # ZDR; each person's audio reaches the bridge socket, speaker events reach the webhook. No transcription.
    return {
        "retention": None,
        "audio_separate_raw": {},
        "realtime_endpoints": [
            {
                "type": "webhook",
                "url": f"{settings.PUBLIC_API_BASE}/api/v1/webhooks/recall/realtime",
                "events": _SPEECH_EVENTS,
            },
            {
                "type": "websocket",
                "url": audio_url(room_id, _ticket(room_id, meeting, livekit_huddles.AUDIO_TICKET)),
                "events": _AUDIO_EVENTS,
            },
        ],
    }


async def create(*, room_id: str, meeting: dict, company: str) -> str:
    """Books the bot; returns its id. Recall does not deduplicate a create, so callers run
    ``find_for_room`` first: ``meeting.ticketNonce`` rides in the metadata as the booking's id."""
    join_at = int(meeting["startAt"]) - 120
    body = {
        "meeting_url": meeting["meetingUrl"],
        "bot_name": company[: 100 - len(_BOT_SUFFIX)] + _BOT_SUFFIX,
        "recording_config": _recording_config(room_id, meeting),
        "output_media": _output_media(room_id, meeting),
        "variant": {"google_meet": "web_4_core"},
        "automatic_leave": {"waiting_room_timeout": 180, "noone_joined_timeout": 1200},
        "chat": {"on_bot_join": {"send_to": "everyone", "message": CONSENT_LINE.format(company=company), "pin": True}},
        "metadata": {"roomId": room_id, "nonce": meeting["ticketNonce"]},
    }
    if join_at > time.time():
        body["join_at"] = _iso(join_at)  # omitted when already past: Recall joins now
    bot = await _call("POST", "/api/v1/bot/", body, idempotency_key=meeting["ticketNonce"])
    if not isinstance(bot, dict) or not bot.get("id"):
        raise RecallError("bad_response")
    return bot["id"]


async def find_for_room(room_id: str, nonce: str) -> str | None:
    """The live bot an earlier create booked for this booking (room + nonce), whose answer was lost
    or whose process died before the id was written; None when there is none to adopt."""
    query = urlencode({"metadata__roomId": room_id, "metadata__nonce": nonce})
    page = await _call("GET", f"/api/v1/bot/?{query}")
    for bot in page.get("results") or []:
        changes = bot.get("status_changes") or [{}]
        if bot.get("id") and changes[-1].get("code") not in _ENDED:
            return str(bot["id"])
    return None


async def reschedule(bot_id: str, *, room_id: str, meeting: dict) -> None:
    """Moves a not-yet-joined bot; both tickets are re-minted so they die with the new end. Recall
    refuses a ``join_at`` under 10 minutes out (400): the caller then replaces the bot."""
    body = {
        "join_at": _iso(int(meeting["startAt"]) - 120),
        "output_media": _output_media(room_id, meeting),
        "recording_config": _recording_config(room_id, meeting),
    }
    await _call("PATCH", f"/api/v1/bot/{bot_id}/", body)


async def leave(bot_id: str) -> None:
    """DELETE a bot that has not been dispatched; Recall answers 405 once it has, so pull it out."""
    try:
        await _call("DELETE", f"/api/v1/bot/{bot_id}/")
    except RecallError as exc:
        if exc.code != "405":
            raise
        await _call("POST", f"/api/v1/bot/{bot_id}/leave_call/")


def verify_webhook(headers, body: bytes) -> bool:
    """Recall's Svix-style signature: ``v1,<b64 HMAC-SHA256(id.timestamp.body)>`` under the
    ``whsec_`` workspace secret, any of the space-separated signatures (rotation), 5-min skew."""
    msg_id, stamp, sigs = (headers.get(f"webhook-{k}") for k in ("id", "timestamp", "signature"))
    secret = settings.RECALL_WEBHOOK_SECRET
    if (
        not (secret and msg_id and stamp and sigs)
        or not stamp.isdigit()
        or abs(time.time() - int(stamp)) > _WEBHOOK_TOLERANCE_S
    ):
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(
        version == "v1" and hmac.compare_digest(sig, expected)
        for version, _, sig in (s.partition(",") for s in sigs.split())
    )
