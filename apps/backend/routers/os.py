"""Spark (the Mac app) account and referrals, keyed on the person, never an organisation.

The app signs in with Clerk's own SDK and sends its Clerk session token, like the web app. A person's Spark
account is a few attributes on their users row: `os_trial_ends_at`, `os_created_at`, `os_referrals` (people
they referred), `os_referral_days` (days they earned by referring; rows from before it existed earned one per
referral), `os_referred_by` and `os_referral_credited` (whose code they came with, and whether it was credited; a
web sign-up stores `os_referred_by` before the account exists), `os_invite_bonus_days` (1 when a paying inviter's
day went on their own trial), and for voice `os_voice_active` (their open session ids) and
`os_voice_seconds` (their voice total). After they delete it, `os_deleted_at` is all that remains. Voice sessions themselves are `core.voice.os_voice`."""

import json
import logging
import re
import time
import uuid
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.auth import AuthContext, get_human_session, get_optional_human_session
from core.config import settings
from core.dynamodb import get_table, is_cancelled, run_in_thread, transact, utc_now_iso
from core.observability import product_analytics, spark_events
from core.services import os_billing
from core.services.rate_bucket import RateBucket
from core.voice import os_voice
from core.voice.os_voice import now as _now
from core.voice.rooms import live_configured
from routers._public_ingress import _extract_caller_ip

logger = logging.getLogger(__name__)
router = APIRouter()

DAY_S = 86400
TRIAL_S = 3 * DAY_S
REFERRAL_WINDOW_S = 14 * DAY_S
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")


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
    checkoutUrl: str | None = None
    portalUrl: str | None = None
    priceText: str
    trialDays: int
    inviteBonusDays: int = 0
    nextChargeAt: int | None = None


class ConfigResponse(BaseModel):
    priceText: str
    trialDays: int


def _price_facts() -> ConfigResponse:
    return ConfigResponse(priceText=f"${settings.SPARK_PRICE_USD}/month", trialDays=TRIAL_S // DAY_S)


@router.get("/config", response_model=ConfigResponse, operation_id="os_config")
async def get_config() -> ConfigResponse:
    """Spark's price and trial length, for the sign-in screen before anyone is signed in. Public."""
    return _price_facts()


class ReferralResponse(BaseModel):
    applied: bool


class VoiceSessionResponse(BaseModel):
    sessionId: str
    sdp: str
    expiresAt: int | None = None


class VoiceSessionStatus(BaseModel):
    status: Literal["active", "ended"]
    endedReason: Literal["client_closed", "trial_ended", "signed_out", "expired", "upstream", "replaced"] | None = None


_DEFAULTS = {":trial": "os_trial_ends_at", ":now": "os_created_at", ":created": "created_at", ":e": "email"}


async def _account(auth: AuthContext, now: int) -> dict:
    """The person's users row, creating their Spark account (trial ending three days from now) in the same
    single write every read makes: `if_not_exists` makes concurrent first reads agree, and a later read never
    moves the trial. The write returns the row as it was, so exactly one call sees no `os_created_at`: that
    call created the account."""
    values: dict[str, Any] = {":trial": now + TRIAL_S, ":now": now, ":created": utc_now_iso()}
    if auth.email:
        values[":e"] = auth.email
    sets = [f"{_DEFAULTS[k]} = if_not_exists({_DEFAULTS[k]}, {k})" for k in values]
    written = await run_in_thread(
        _table().update_item,
        Key={"user_id": auth.user_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeValues=values,
        ReturnValues="ALL_OLD",
    )
    old = written.get("Attributes") or {}
    row = {_DEFAULTS[k]: v for k, v in values.items()} | old  # every SET is if_not_exists
    if "os_created_at" not in old:
        if "os_deleted_at" in old:  # a deleted Spark account comes back with its trial already over
            row["os_trial_ends_at"] = now
            await run_in_thread(
                _table().update_item,
                Key={"user_id": auth.user_id},
                UpdateExpression="SET os_trial_ends_at = :now",
                ExpressionAttributeValues={":now": now},
            )
        spark_events.server_event(auth.user_id, "account_created", auth.user_id, row=row, now=now)
    # Referred on the web before the account existed: the referrer gets their day now. Checked on every read,
    # not only the creating one, so a failed credit is retried; the condition makes it once.
    if row.get("os_referred_by") and not row.get("os_referral_credited") and "os_deleted_at" not in row:
        try:
            got = await _credit(auth.user_id, row["os_referred_by"], "SET os_referral_credited = :t", _NOT_CREDITED, {})
            if got:
                row["os_referral_credited"] = True
            if got == "friend":
                row["os_trial_ends_at"] = int(row["os_trial_ends_at"]) + DAY_S
                row["os_invite_bonus_days"] = 1
        except Exception:  # noqa: BLE001  a credit never fails the account read
            logger.warning("os referral credit not written for %s", auth.user_id, exc_info=True)
    return row


@router.get("/account", response_model=AccountResponse, operation_id="os_account")
async def get_account(auth: AuthContext = Depends(get_human_session)) -> AccountResponse:
    """The signed-in person's Spark account. The first read creates it."""
    now = _now()
    row = await _account(auth, now)
    ends, referrals = int(row["os_trial_ends_at"]), int(row.get("os_referrals", 0))
    checkout_url, portal_url = os_billing.links(row, auth.user_id, now)
    return AccountResponse(
        accountId=auth.user_id,
        email=row.get("email") or "",
        name="",  # the backend stores no name for a person
        status=os_billing.status(row, now),
        trialEndsAt=ends,
        referralUrl=f"{settings.FRONTEND_URL.rstrip('/')}/os?ref={auth.user_id.removeprefix('user_')}",
        referralDaysEarned=int(row.get("os_referral_days", referrals)),
        referralCount=referrals,
        checkoutUrl=checkout_url,
        portalUrl=portal_url,
        inviteBonusDays=int(row.get("os_invite_bonus_days", 0)),
        nextChargeAt=int(n)
        if row.get("os_subscription_status") in os_billing.LIVE and (n := row.get("os_next_charge_at"))
        else None,
        **_price_facts().model_dump(),
    )


@router.delete("/account", status_code=204, operation_id="os_account_delete")
async def delete_account(auth: AuthContext = Depends(get_human_session)) -> Response:
    """Delete the signed-in person's Spark account. Every step is idempotent, so a retry finishes a partial one:
    cancel their Spark subscription, close their open voice session at OpenAI, remove every `os_*` attribute from their
    users row except the `os_deleted_at` tombstone (a later account starts with no trial), then delete the
    Clerk user. A person in any Clerk organization keeps their Clerk user, since their other Lightbulb product
    still uses it; the answer is 204 all the same. Referrers keep what they earned. Ended voice-session rows
    are keyed by session id and expire by TTL. A Stripe or Clerk failure is 502 `delete_failed`."""
    from core.services import clerk_admin

    uid = auth.user_id
    try:
        await os_billing.cancel_for_deleted_user(uid)  # raises before any Spark data is removed
        for sid in (await os_billing._row(uid)).get(os_voice.ACTIVE) or ():
            await os_voice.close_and_wait(sid, "signed_out")  # OpenAI gets session.close before the cleanup
        row = await os_billing._row(uid)  # re-read: ending a session adds to os_voice_seconds
        spark = {f"#a{i}": k for i, k in enumerate(k for k in row if k.startswith("os_") and k != "os_deleted_at")}
        if spark:
            spark_events.server_event(uid, "account_deleted", uid)
            await run_in_thread(
                _table().update_item,
                Key={"user_id": uid},
                UpdateExpression="SET os_deleted_at = if_not_exists(os_deleted_at, :now) REMOVE " + ", ".join(spark),
                ExpressionAttributeNames=spark,
                ExpressionAttributeValues={":now": _now()},
            )
        if not await clerk_admin.list_user_organizations(uid, limit=100, strict=True):
            await clerk_admin.delete_user(uid)
    except Exception:  # noqa: BLE001  Stripe, Clerk or DynamoDB: the app retries
        logger.warning("os account delete failed for %s", uid, exc_info=True)
        raise HTTPException(status_code=502, detail="delete_failed") from None
    return Response(status_code=204)


def _referrer(code: Any, user_id: str) -> str | None:
    """The user id a referral code names, or None for anything else (including the caller themself).
    ponytail: the code is the Clerk user id without `user_`, an identifier and not a secret; a short vanity
    code would need a lookup row."""
    referrer = f"user_{code}" if isinstance(code, str) else ""
    return referrer if _USER_ID.fullmatch(referrer) and referrer != user_id else None


def _item(kind: str, user_id: str, cond: str, values: dict, expr: str | None = None) -> dict:
    item: dict[str, Any] = {"TableName": _table().name, "Key": {"user_id": user_id}, "ConditionExpression": cond}
    if expr:
        item["UpdateExpression"] = expr
    if values:
        item["ExpressionAttributeValues"] = values
    return {kind: item}


async def _written(items: list[dict]) -> bool:
    """One transaction: True when written, False when a condition refused it. Anything else raises."""
    try:
        await transact(_table(), items)
    except ClientError as e:
        reasons = e.response.get("CancellationReasons") or []
        if is_cancelled(e) and any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
            return False
        raise
    return True


_NEVER_DELETED = "attribute_not_exists(os_deleted_at)"  # a person who deleted Spark is never a referee again
_NOT_CREDITED = f"attribute_not_exists(os_referral_credited) AND {_NEVER_DELETED}"


_ST = "os_subscription_status"
_EARNS = "SET os_referral_days = if_not_exists(os_referral_days, :prior) + :one"
# Who gets the day, by the referrer's row: each condition pins one branch, and the three cover every row.
_BRANCHES = {
    # Paying: the friend gets the day on their own trial; the referrer's trial and billing are untouched.
    "friend": (
        f"attribute_exists(os_trial_ends_at) AND {_ST} = :active",
        "SET os_referral_days = if_not_exists(os_referral_days, :prior) ADD os_referrals :one",
    ),
    # A running trial, or a trialing subscription: the day goes on the end, and `extend_trial` moves Stripe's.
    "referrer": (
        f"attribute_exists(os_trial_ends_at) AND (attribute_not_exists({_ST}) OR {_ST} <> :active)"
        f" AND (os_trial_ends_at > :now OR {_ST} = :trialing)",
        f"{_EARNS} ADD os_trial_ends_at :day, os_referrals :one",
    ),
    # An ended trial with no live subscription: the day counts from now.
    "ended": (
        f"os_trial_ends_at <= :now AND (attribute_not_exists({_ST}) OR NOT {_ST} IN (:active, :trialing))",
        f"{_EARNS}, os_trial_ends_at = :until ADD os_referrals :one",
    ),
}


def _branch(ref: dict, now: int) -> str:
    if ref.get(_ST) == "active":
        return "friend"
    return "referrer" if ref.get(_ST) == "trialing" or int(ref["os_trial_ends_at"]) > now else "ended"


async def _credit(user_id: str, referrer: str, expr: str, cond: str, values: dict) -> str:
    """Both rows or neither: the referred person is marked credited (`expr` under `cond`) and the referrer, who
    must have a Spark account, counts one more referral. One friend is one free day: a paying (`active`)
    referrer's friend gets it on their own trial; anyone else earns it, on a running trial's end or, once the
    trial has ended, from now (an ADD cannot say max(end, now)). The branch comes from the referrer's row read
    first and is pinned by its condition; a referrer who crossed a line meanwhile is read again, once.
    Returns "friend" or "referrer" (who got the day), or "" when nothing was written."""
    for _ in range(2):
        now = _now()
        ref = (await run_in_thread(_table().get_item, Key={"user_id": referrer}, ConsistentRead=True)).get("Item")
        if not ref or "os_trial_ends_at" not in ref:
            return ""
        branch = _branch(ref, now)
        ref_cond, ref_expr = _BRANCHES[branch]
        every = {":active": "active", ":trialing": "trialing", ":now": now, ":until": now + DAY_S, ":day": DAY_S}
        every |= {":prior": int(ref.get("os_referrals", 0)), ":one": 1}
        ref_values = {k: every[k] for k in set(re.findall(r":\w+", f"{ref_cond} {ref_expr}"))}
        friend_expr, friend_values = expr, values | {":t": True}
        if branch == "friend":
            friend_expr += ", os_invite_bonus_days = :one ADD os_trial_ends_at :day"
            friend_values |= {":one": 1, ":day": DAY_S}
        try:
            await transact(
                _table(),
                [
                    _item("Update", user_id, cond, friend_values, friend_expr),
                    _item("Update", referrer, ref_cond, ref_values, ref_expr),
                ],
            )
        except ClientError as e:
            codes = [r.get("Code") for r in e.response.get("CancellationReasons") or []]
            if not is_cancelled(e) or "ConditionalCheckFailed" not in codes:
                raise
            if codes[0] == "ConditionalCheckFailed":  # already credited, or not creditable
                return ""
            continue  # the referrer's row moved across a branch line
        if branch != "friend":
            await os_billing.extend_trial(referrer)
        spark_events.server_event(referrer, "referral_applied", user_id)
        return "friend" if branch == "friend" else "referrer"
    return ""


@router.post("/referral", response_model=ReferralResponse, operation_id="os_referral")
async def apply_referral(body: Any = Body(None), auth: AuthContext = Depends(get_human_session)) -> ReferralResponse:
    """Whose code the signed-in person came with, once per person, and only for another person with a Spark
    account. With a Spark account under 14 days old, the referrer gets their day now. With no Spark account
    yet (a web sign-up), only the code is stored; the referrer gets their day when the account is created
    (`_account`). Every refusal is the same `applied: false`, so the route says nothing about whether a
    person exists."""
    referrer = _referrer(body.get("code") if isinstance(body, dict) else None, auth.user_id)
    if not referrer:
        return ReferralResponse(applied=False)
    try:
        got = await run_in_thread(_table().get_item, Key={"user_id": auth.user_id}, ConsistentRead=True)
        if "os_created_at" in (got.get("Item") or {}):
            applied = bool(
                await _credit(
                    auth.user_id,
                    referrer,
                    "SET os_referred_by = :ref, os_referral_credited = :t",
                    f"attribute_not_exists(os_referred_by) AND os_created_at > :cutoff AND {_NEVER_DELETED}",
                    {":ref": referrer, ":cutoff": _now() - REFERRAL_WINDOW_S},
                )
            )
        else:
            applied = await _written(
                [
                    _item("ConditionCheck", referrer, "attribute_exists(os_trial_ends_at)", {}),
                    _item(
                        "Update",
                        auth.user_id,
                        f"attribute_not_exists(os_referred_by) AND attribute_not_exists(os_created_at) AND {_NEVER_DELETED}",
                        {":ref": referrer},
                        "SET os_referred_by = :ref",
                    ),
                ]
            )
    except ClientError as e:
        logger.warning("os referral not written for %s: %s", auth.user_id, e.response.get("Error", {}).get("Code"))
        raise HTTPException(status_code=503, detail="referral_unavailable")
    except BotoCoreError:
        logger.warning("os referral not written for %s: transport", auth.user_id)
        raise HTTPException(status_code=503, detail="referral_unavailable")
    if applied:
        spark_events.server_event(auth.user_id, "referred_by_applied", auth.user_id)
    return ReferralResponse(applied=applied)


MODELS = ("gpt-live-1.1", "gpt-live-1")  # the first is the default
VOICES = frozenset(
    "alloy ash ballad beacon bossa cedar cinder coral delta echo gleam marin meridian quartz ripple sage shimmer "
    "stone tempo verse vesper willow".split()
)
KINDS = ("hold", "handsFree", "typed", "wakeWord")
INSTRUCTIONS_MAX_BYTES = 65_536  # OpenAI enforces the exact 16,384-token limit; its refusal is invalid_config
INPUT_MAX, INPUT_MAX_BYTES = 128, 65_536  # items; total text of all items
SDP_MAX = 64 * 1024
# Each `input` item's role and the part types its one text part may have (the Live create reference).
_PART_TYPES = {"developer": {"input_text"}, "user": {"input_text"}, "assistant": {"text", "output_text"}}
_SESSION_ID = re.compile(r"vs_[0-9a-f]{32}")


def _bad(code: str) -> HTTPException:
    return HTTPException(status_code=400, detail=code)


def _input_text(item: Any) -> str | None:
    """The text of one startup history message, or None when it is not the documented shape."""
    if not isinstance(item, dict) or not set(item) <= {"role", "content", "id", "status", "type"}:
        return None
    parts, role = item.get("content"), item.get("role")
    part = parts[0] if isinstance(parts, list) and len(parts) == 1 else None
    if (
        not isinstance(role, str)
        or role not in _PART_TYPES
        or item.get("type", "message") != "message"
        or item.get("status") not in (None, "incomplete", "completed")
        or not isinstance(item.get("id", ""), str | None)
        or not isinstance(part, dict)
        or not set(part) <= {"text", "type"}
        or not isinstance(part_type := part.get("type", "text" if role == "assistant" else "input_text"), str)
        or part_type not in _PART_TYPES[role]
        or not isinstance(part.get("text"), str)
    ):
        return None
    return part["text"]


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
        texts = [_input_text(i) for i in items] if isinstance(items, list) and len(items) <= INPUT_MAX else [None]
        if None in texts or sum(len(t.encode()) for t in texts) > INPUT_MAX_BYTES:
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


def _client(client: Any) -> dict:
    """The app's description of itself, stored with the session (never sent to OpenAI): its version and
    protocol as short strings, which assistant it runs (`brain`) and whether it has full access. Unknown keys
    and known keys with a bad value are dropped."""
    client = client if isinstance(client, dict) else {}
    out = {k: str(client[k])[:64] for k in ("appVersion", "protocol") if k in client}
    if client.get("brain") in ("chatgpt", "claude"):
        out["brain"] = client["brain"]
    if type(client.get("fullAccess")) is bool:
        out["fullAccess"] = client["fullAccess"]
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
    if not isinstance(model, str) or model not in MODELS:
        raise _bad("model_not_allowed")
    kind = body.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in KINDS
        or type(body.get("dcid", 4)) is not int
        or body.get("dcid", 4) != 4
    ):
        raise _bad("invalid_config")
    config = _live_session(body.get("session", {}))
    client = _client(body.get("client", {}))
    if not live_configured():
        raise HTTPException(status_code=503, detail="voice_unavailable")
    at = _now()
    if os_billing.status(await _account(auth, at), at) == "ended":
        raise HTTPException(status_code=402, detail="trial_ended")
    return await os_voice.open_session(auth.user_id, kind, client, model, config, sdp)


@router.get("/voice-session/{session_id}", response_model=VoiceSessionStatus, operation_id="os_voice_session_status")
async def voice_session_status(session_id: str, auth: AuthContext = Depends(get_human_session)) -> VoiceSessionStatus:
    """Whether the caller's session is still open, and why it ended. Another person's session and an unknown
    id are the same 404."""
    row = await os_voice.get(session_id) if _SESSION_ID.fullmatch(session_id) else None
    if not row or row.get("userId") != auth.user_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.get("status") == os_voice.ACTIVE:
        return VoiceSessionStatus(status="active")
    return VoiceSessionStatus(status="ended", endedReason=row.get("endedReason"))


@router.delete("/voice-session/{session_id}", status_code=204, operation_id="os_voice_session_close")
async def close_voice_session(session_id: str, auth: AuthContext = Depends(get_human_session)) -> Response:
    """The app will not use this session: close it now rather than wait for OpenAI to drop it. Answers at
    once; the sideband closes it on its next tick. Ended or already closing is 204 too; another person's
    session and an unknown id are the GET's 404."""
    row = await os_voice.get(session_id) if _SESSION_ID.fullmatch(session_id) else None
    if not row or row.get("userId") != auth.user_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.get("status") == os_voice.ACTIVE:
        await os_voice.request_close(session_id)
    return Response(status_code=204)


EVENT_MAX_BYTES = 4096
INSTALL_IDS_MAX = 10
# ponytail: in-process buckets, per backend task; at 2 to 4 tasks the real ceiling is 2 to 4 times these.
# Move to a shared counter (a DynamoDB per-minute item) if abuse ever shows.
_PER_ID = RateBucket(60)
_PER_IP = RateBucket(600)


def _install_id(value: Any) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


async def _link_install(user_id: str, install_id: str) -> bool:
    """Remember the install on the person's existing users row, at most INSTALL_IDS_MAX of them. True only
    when this call added it."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="ADD os_install_ids :iid",
            ConditionExpression="attribute_exists(user_id) AND (attribute_not_exists(os_install_ids) OR "
            "(NOT contains(os_install_ids, :one) AND size(os_install_ids) < :cap))",
            ExpressionAttributeValues={":iid": {install_id}, ":one": install_id, ":cap": INSTALL_IDS_MAX},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("os install link not written: %s", e.response["Error"]["Code"])
        return False
    except BotoCoreError:
        return False
    return True


@router.post("/event", status_code=204, operation_id="os_event")
async def post_event(request: Request, auth: AuthContext | None = Depends(get_optional_human_session)) -> Response:
    """One product event from the app, for PostHog. Anyone may call it: with a valid session bearer the event
    is the person's, otherwise the install's. Only allow-listed events and properties are sent
    (`spark_events.APP_EVENTS`). A well-formed body is always 204, sent, dropped or rate-limited alike."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > EVENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_large")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > EVENT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="too_large")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    install_id = _install_id(body.get("installId")) if isinstance(body, dict) else None
    if not install_id:
        raise HTTPException(status_code=400, detail="invalid_event")
    distinct_id = auth.user_id if auth else install_id
    if (not auth and not _PER_IP.allow(_extract_caller_ip(request))) or not _PER_ID.allow(distinct_id):
        return Response(status_code=204)
    try:
        if auth and await _link_install(auth.user_id, install_id):
            product_analytics.alias_product_person(person_id=auth.user_id, anonymous_id=install_id)
        event = body.get("event")
        if isinstance(event, str) and event in spark_events.APP_EVENTS:
            props = spark_events.allowed_properties(event, body.get("properties"))
            props |= {"install_id": install_id, "product": spark_events.PRODUCT}
            for key, prop in (("appVersion", "app_version"), ("osVersion", "os_version")):
                if isinstance(v := body.get(key), str) and spark_events.VERSION.fullmatch(v):
                    props[prop] = v
            day = time.strftime("%Y-%m-%d", time.gmtime())
            once = str(uuid.uuid5(spark_events.NS, f"app_opened:{install_id}:{day}")) if event == "app_opened" else None
            product_analytics.capture_product_event(
                distinct_id=distinct_id, event=event, properties=props, event_uuid=once
            )
    except Exception:  # noqa: BLE001  analytics never fails the app
        logger.warning("os event not captured", exc_info=True)
    return Response(status_code=204)
