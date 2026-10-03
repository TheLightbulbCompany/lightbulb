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

import boto3
import httpx

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import livekit_huddles

CONSENT_LINE = (
    "Lightbulb agents are in this meeting for {company}. They listen for their names; notes and a "
    "transcript go to {company}'s Lightbulb workspace. Recall.ai relays the call and stores nothing."
)
_REASONS = {
    "402": "the meeting-bot account is out of credit",
    "507": "no meeting bots are free right now",
    "timeout": "the meeting-bot service didn't answer",
}
_TICKET_GRACE_S = 4 * 3600  # the ticket outlives the scheduled end by this much, then dies
_WEBHOOK_TOLERANCE_S = 300
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport
_secrets: dict[str, str] = {}


class RecallError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.reason = _REASONS.get(code, f"the meeting-bot service answered {code}")


async def _secret(name: str) -> str:
    """``lightbulb/{env}/{name}``, read once per process."""
    if name not in _secrets:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        resp = await run_in_thread(sm.get_secret_value, SecretId=f"lightbulb/{settings.ENVIRONMENT}/{name}")
        _secrets[name] = resp["SecretString"].strip()
    return _secrets[name]


async def _call(method: str, path: str, body: dict | None = None, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Token {await _secret('recall_api_key')}"}
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
    return resp.json() if resp.content else {}


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _output_media(room_id: str, meeting: dict) -> dict:
    exp = int(meeting.get("endAt") or meeting["startAt"]) + _TICKET_GRACE_S
    ticket = livekit_huddles.meet_ticket(room_id, meeting["ticketNonce"], exp)
    page = settings.MEET_BOT_PAGE_URL or f"{settings.FRONTEND_URL.rstrip('/')}/meet-bot"
    return {"camera": {"kind": "webpage", "config": {"url": f"{page}#t={ticket}"}}}


async def create(*, room_id: str, meeting: dict, company: str) -> str:
    """Books the bot; returns its id. ``meeting.ticketNonce`` is both the page ticket's binding and
    the Idempotency-Key, so a retry after a lost response gets the same bot back (Recall: 1 h)."""
    join_at = int(meeting["startAt"]) - 120
    body = {
        "meeting_url": meeting["meetingUrl"],
        "bot_name": f"{company} agents · Lightbulb",
        # ponytail: audio_separate_raw + realtime_endpoints join recording_config once Recall enables
        # per-participant audio for the workspace (Task 0).
        "recording_config": {"retention": None},  # ZDR
        "output_media": _output_media(room_id, meeting),
        "variant": {"google_meet": "web_4_core"},
        "automatic_leave": {"waiting_room_timeout": 600, "noone_joined_timeout": 300},
        "chat": {"on_bot_join": {"send_to": "everyone", "message": CONSENT_LINE.format(company=company), "pin": True}},
        "metadata": {"roomId": room_id},
    }
    if join_at > time.time():
        body["join_at"] = _iso(join_at)  # omitted when already past: Recall joins now
    return (await _call("POST", "/api/v1/bot/", body, idempotency_key=meeting["ticketNonce"]))["id"]


async def reschedule(bot_id: str, *, room_id: str, meeting: dict) -> None:
    """Moves a not-yet-joined bot; the page ticket is re-minted so it dies with the new end. Recall
    refuses a ``join_at`` under 10 minutes out (400): the caller then replaces the bot."""
    body = {"join_at": _iso(int(meeting["startAt"]) - 120), "output_media": _output_media(room_id, meeting)}
    await _call("PATCH", f"/api/v1/bot/{bot_id}/", body)


async def leave(bot_id: str) -> None:
    """DELETE a bot that has not been dispatched; Recall answers 405 once it has, so pull it out."""
    try:
        await _call("DELETE", f"/api/v1/bot/{bot_id}/")
    except RecallError as exc:
        if exc.code != "405":
            raise
        await _call("POST", f"/api/v1/bot/{bot_id}/leave_call/")


async def verify_webhook(headers, body: bytes) -> bool:
    """Recall's Svix-style signature: ``v1,<b64 HMAC-SHA256(id.timestamp.body)>`` under the
    ``whsec_`` workspace secret, any of the space-separated signatures (rotation), 5-min skew."""
    msg_id, stamp, sigs = (headers.get(f"webhook-{k}") for k in ("id", "timestamp", "signature"))
    if not (msg_id and stamp and sigs) or not stamp.isdigit() or abs(time.time() - int(stamp)) > _WEBHOOK_TOLERANCE_S:
        return False
    key = base64.b64decode((await _secret("recall_webhook_secret")).removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(
        version == "v1" and hmac.compare_digest(sig, expected)
        for version, _, sig in (s.partition(",") for s in sigs.split())
    )
