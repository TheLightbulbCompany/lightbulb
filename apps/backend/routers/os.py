"""Spark (the Mac app) account and referrals, keyed on the person, never an organisation.

The app signs in with Clerk's own SDK and sends its Clerk session token, like the web app. A person's Spark
account is a few attributes on their users row: `os_trial_ends_at`, `os_created_at`, `os_referrals` (people
they referred), `os_referred_by`, and for voice `os_voice_active` (their open session ids) and
`os_voice_seconds` (their voice total). Voice sessions themselves are `core.voice.os_voice`."""

import hashlib
import logging
import re
import time
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_human_session
from core.config import settings
from core.dynamodb import get_table, is_cancelled, run_in_thread, transact, utc_now_iso
from core.voice import os_voice
from core.voice.rooms import _live_configured

logger = logging.getLogger(__name__)
router = APIRouter()

DAY_S = 86400
TRIAL_S = 3 * DAY_S
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")


def _now() -> int:
    return int(time.time())


def _table():
    return get_table("users")


class AccountResponse(BaseModel):
    accountId: str
    email: str
    name: str
    status: str
    trialEndsAt: int
    referralUrl: str
    referralDaysEarned: int
    referralCount: int
    checkoutUrl: None = None
    portalUrl: None = None


class ReferralResponse(BaseModel):
    applied: bool


class VoiceSessionResponse(BaseModel):
    sessionId: str
    sdp: str
    expiresAt: int | None = None


class VoiceSessionStatus(BaseModel):
    status: str
    endedReason: str | None = None


async def _account(auth: AuthContext, now: int) -> dict:
    """The person's users row, creating their Spark account (trial ending three days from now) in the same
    single write every read makes: `if_not_exists` makes concurrent first reads agree, and a later read never
    moves the trial."""
    sets = [
        "os_trial_ends_at = if_not_exists(os_trial_ends_at, :trial)",
        "os_created_at = if_not_exists(os_created_at, :now)",
        "created_at = if_not_exists(created_at, :created)",
    ]
    values: dict[str, Any] = {":trial": now + TRIAL_S, ":now": now, ":created": utc_now_iso()}
    if auth.email:
        sets.append("email = if_not_exists(email, :e)")
        values[":e"] = auth.email
    written = await run_in_thread(
        _table().update_item,
        Key={"user_id": auth.user_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return written["Attributes"]


@router.get("/account", response_model=AccountResponse, operation_id="os_account")
async def get_account(auth: AuthContext = Depends(get_human_session)) -> AccountResponse:
    """The signed-in person's Spark account. The first read creates it."""
    now = _now()
    row = await _account(auth, now)
    ends, referrals = int(row["os_trial_ends_at"]), int(row.get("os_referrals", 0))
    return AccountResponse(
        accountId=auth.user_id,
        email=row.get("email") or "",
        name="",  # the backend stores no name for a person
        status="trial" if now < ends else "ended",
        trialEndsAt=ends,
        referralUrl=f"{settings.FRONTEND_URL.rstrip('/')}/os?ref={auth.user_id.removeprefix('user_')}",
        referralDaysEarned=referrals,  # one referral is one day
        referralCount=referrals,
    )


def _referrer(code: Any, user_id: str) -> str | None:
    """The user id a referral code names, or None for anything else (including the caller themself).
    ponytail: the code is the Clerk user id without `user_`, an identifier and not a secret; a short vanity
    code would need a lookup row."""
    referrer = f"user_{code}" if isinstance(code, str) else ""
    return referrer if _USER_ID.fullmatch(referrer) and referrer != user_id else None


@router.post("/referral", response_model=ReferralResponse, operation_id="os_referral")
async def apply_referral(body: Any = Body(None), auth: AuthContext = Depends(get_human_session)) -> ReferralResponse:
    """One more day for the person whose code this is. Applies once per referred person, only to a Spark
    account under a day old, and only for another person with a Spark account. Every refusal is the same
    `applied: false`, so the route says nothing about whether a person exists."""
    referrer = _referrer(body.get("code") if isinstance(body, dict) else None, auth.user_id)
    if not referrer:
        return ReferralResponse(applied=False)
    table = _table()

    def update(user_id: str, expr: str, cond: str, values: dict) -> dict:
        return {
            "Update": {
                "TableName": table.name,
                "Key": {"user_id": user_id},
                "UpdateExpression": expr,
                "ConditionExpression": cond,
                "ExpressionAttributeValues": values,
            }
        }

    try:
        # Both rows or neither: the referred person is marked once, and the referrer is credited with it.
        await transact(
            table,
            [
                update(
                    auth.user_id,
                    "SET os_referred_by = :ref",
                    "attribute_not_exists(os_referred_by) AND os_created_at > :cutoff",
                    {":ref": referrer, ":cutoff": _now() - DAY_S},
                ),
                update(
                    referrer,
                    "ADD os_trial_ends_at :day, os_referrals :one",
                    "attribute_exists(os_trial_ends_at)",
                    {":day": DAY_S, ":one": 1},
                ),
            ],
        )
    except ClientError as e:
        reasons = e.response.get("CancellationReasons") or []
        if is_cancelled(e) and any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
            return ReferralResponse(applied=False)
        logger.warning("os referral not written for %s: %s", auth.user_id, e.response.get("Error", {}).get("Code"))
        raise HTTPException(status_code=503, detail="referral_unavailable")
    except BotoCoreError:
        logger.warning("os referral not written for %s: transport", auth.user_id)
        raise HTTPException(status_code=503, detail="referral_unavailable")
    return ReferralResponse(applied=True)


MODELS = ("gpt-live-1.1", "gpt-live-1")  # the first is the default
VOICES = frozenset(
    "alloy ash ballad beacon bossa cedar cinder coral delta echo gleam marin meridian quartz ripple sage shimmer "
    "stone tempo verse vesper willow".split()
)
KINDS = ("hold", "handsFree", "typed", "wakeWord")
ALLOWED_CLIENT_EVENTS = [
    "session.input_audio.mute",
    "session.input_audio.unmute",
    "session.instructions.append",
    "session.thinking.append",
    "session.commentary.append",
    "session.close",
]
# OpenAI's limit is 16,384 tokens. A token covers at least one UTF-8 byte, so 16,384 bytes is never over it.
INSTRUCTIONS_MAX_BYTES = 16_384
INPUT_MAX = 128
SDP_MAX = 64 * 1024


def _bad(code: str) -> HTTPException:
    return HTTPException(status_code=400, detail=code)


def _live_session(session: Any) -> dict:
    """The client's part of the session configuration, field by field. Anything else is refused, so the
    client cannot add tools, turn on `store` or set any other session option."""
    if not isinstance(session, dict) or not set(session) <= {"instructions", "input", "audio", "delegation"}:
        raise _bad("invalid_config")
    out: dict[str, Any] = {}
    if (text := session.get("instructions")) is not None:
        if not isinstance(text, str) or len(text.encode()) > INSTRUCTIONS_MAX_BYTES:
            raise _bad("invalid_config")
        out["instructions"] = text
    if "input" in session:
        items = session["input"]
        if not isinstance(items, list) or len(items) > INPUT_MAX or not all(isinstance(i, dict) for i in items):
            raise _bad("invalid_config")
        out["input"] = items
    if "audio" in session:
        audio = session["audio"]
        output = audio.get("output") if isinstance(audio, dict) and set(audio) == {"output"} else None
        voice = output.get("voice") if isinstance(output, dict) and set(output) == {"voice"} else None
        if not isinstance(voice, str) or voice not in VOICES:
            raise _bad("invalid_config")
        out["audio"] = {"output": {"voice": voice}}
    if "delegation" in session:
        d = session["delegation"]
        delay = d.get("acknowledgement_delay_ms", 0) if isinstance(d, dict) else None
        if (
            not isinstance(d, dict)
            or d.get("type") != "client"
            or not set(d) <= {"type", "acknowledgement_delay_ms"}
            or type(delay) is not int
            or not 0 <= delay <= 800
        ):
            raise _bad("invalid_config")
        out["delegation"] = d
    return out


@router.post("/voice-session", status_code=201, response_model=VoiceSessionResponse, operation_id="os_voice_session")
async def create_voice_session(
    body: Any = Body(None), auth: AuthContext = Depends(get_human_session)
) -> VoiceSessionResponse | JSONResponse:
    """A GPT-Live WebRTC session on the company key: the Mac's SDP offer in, OpenAI's answer out. The backend
    owns the model, `store`, the voice list, the size caps, the transport and the data channel's allowed
    events. Reconnecting is a new POST."""
    if not isinstance(body, dict):
        raise _bad("invalid_config")
    sdp = body.get("sdp")
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > SDP_MAX:
        raise _bad("invalid_sdp")
    model = body.get("model", MODELS[0])
    if model not in MODELS:
        raise _bad("model_not_allowed")
    kind = body.get("kind")
    if kind not in KINDS or body.get("dcid", 4) != 4:
        raise _bad("invalid_config")
    config = _live_session(body.get("session", {}))
    client = body.get("client") if isinstance(body.get("client"), dict) else {}
    client = {k: str(client[k])[:64] for k in ("appVersion", "protocol") if k in client}
    if not _live_configured():
        raise HTTPException(status_code=503, detail="voice_unavailable")
    now = _now()
    if now >= int((await _account(auth, now))["os_trial_ends_at"]):
        raise HTTPException(status_code=402, detail="trial_ended")
    sid = f"vs_{uuid.uuid4().hex}"
    if not await os_voice.reserve(auth.user_id, sid, kind, client, now):
        raise HTTPException(status_code=429, detail="too_many_sessions")
    config |= {
        "model": model,
        "store": False,
        "client": {"data_channel": {"allowed_client_events": ALLOWED_CLIENT_EVENTS}},
        # OpenAI's abuse detection per end user, without naming them.
        "safety_identifier": hashlib.sha256(auth.user_id.encode()).hexdigest(),
    }
    transport = {"type": "webrtc", "sdp": sdp, "dcid": 4}  # dcid in the body: OpenAI rejects query parameters
    try:
        live_id, answer = await os_voice.create_live({"session": config, "transport": transport})
    except os_voice.Upstream as e:
        await os_voice.end_session(sid, "upstream", 0)
        logger.warning("os voice session %s for %s not created: %s", sid, auth.user_id, e.status)
        if e.status == 429:
            return JSONResponse(
                status_code=429,
                content={"detail": "capacity", "retryAfterS": e.retry_after},
                headers={"Retry-After": str(e.retry_after)},
            )
        raise HTTPException(status_code=e.status, detail="upstream")
    await os_voice.set_live(sid, live_id)
    os_voice.start_sideband(sid)
    logger.info(
        "os voice session %s (%s) opened for %s: %s %s, offer %d bytes",
        sid,
        live_id,
        auth.user_id,
        kind,
        model,
        len(sdp),
    )
    return VoiceSessionResponse(sessionId=sid, sdp=answer, expiresAt=None)


@router.get("/voice-session/{session_id}", response_model=VoiceSessionStatus, operation_id="os_voice_session_status")
async def voice_session_status(session_id: str, auth: AuthContext = Depends(get_human_session)) -> VoiceSessionStatus:
    """Whether the caller's session is still open, and why it ended. Another person's session and an unknown
    id are the same 404."""
    row = await os_voice.get(session_id) if len(session_id) <= 64 else None
    if not row or row.get("userId") != auth.user_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.get("status") == os_voice.ACTIVE:
        return VoiceSessionStatus(status="active")
    return VoiceSessionStatus(status="ended", endedReason=row.get("endedReason"))
