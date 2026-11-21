"""Spark (the Mac app) sign-in and account status, keyed on the person, never an organisation.

The browser trades a Clerk session for a one-time ticket (POST /os/ticket); the app trades the ticket for an
app token (POST /os/session) and sends that token on every later call. Both are `<user_id>.<secret>`: the
users row of `user_id` holds the SHA-256 of each secret (`os_ticket`, `os_tokens`) and the trial end
(`os_trial_ends_at`), so a lookup is one GetItem on the key the credential names."""

import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from core.auth import AuthContext, get_human_session
from core.config import settings
from core.dynamodb import get_table, run_in_thread, utc_now_iso

logger = logging.getLogger(__name__)
router = APIRouter()

TICKET_TTL_S = 60
TRIAL_S = 3 * 86400
MAX_TOKENS = 10
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")
_bearer = HTTPBearer(auto_error=False)


def _now() -> int:
    return int(time.time())


def _table():
    return get_table("users")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _mint(user_id: str) -> tuple[str, str]:
    """(credential, hash of its secret)."""
    secret = secrets.token_urlsafe(32)
    return f"{user_id}.{secret}", _hash(secret)


def _split(credential: str) -> tuple[str, str] | None:
    """(user_id, hash of the secret), or None when the credential is not one we mint."""
    user_id, _, secret = credential.partition(".")
    if not secret or not _USER_ID.fullmatch(user_id):
        return None
    return user_id, _hash(secret)


class TicketRequest(BaseModel):
    ref: Any = None  # a referral code; anything that is not one is ignored, never refused


class TicketResponse(BaseModel):
    ticket: str


class SessionRequest(BaseModel):
    ticket: str


class SessionResponse(BaseModel):
    token: str
    accountId: str
    expiresAt: None = None


class AccountResponse(BaseModel):
    accountId: str
    email: str
    name: str
    status: str
    trialEndsAt: int
    checkoutUrl: None = None
    portalUrl: None = None
    referralUrl: str
    referralDaysEarned: int
    referralCount: int


def _referrer(ref: Any, user_id: str) -> str | None:
    """The user id a referral code names, or None for anything else (including the caller themself).
    ponytail: the code is the Clerk user id without `user_`, an identifier and not a secret; a short vanity
    code would need a lookup row."""
    referrer = f"user_{ref}" if isinstance(ref, str) else ""
    return referrer if _USER_ID.fullmatch(referrer) and referrer != user_id else None


async def _credit(referrer: str) -> None:
    """One more day and one more referral for a person who has a Spark account; anyone else is left as is."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"user_id": referrer},
            UpdateExpression="ADD os_trial_ends_at :day, os_referrals :one",
            ConditionExpression="attribute_exists(os_trial_ends_at)",
            ExpressionAttributeValues={":day": 86400, ":one": 1},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


@router.post("/ticket", response_model=TicketResponse, operation_id="os_ticket")
async def create_ticket(
    body: TicketRequest | None = None, auth: AuthContext = Depends(get_human_session)
) -> TicketResponse:
    """A one-time ticket for the signed-in person, valid 60 seconds. Creates the person's account (trial
    ending three days from now) on first sign-in; a later sign-in never moves the trial. A referral code on
    the sign-in that creates the account gives the referrer one more day."""
    ticket, h = _mint(auth.user_id)
    now = _now()
    sets = ["os_ticket = :t", "os_trial_ends_at = if_not_exists(os_trial_ends_at, :trial)"]
    sets.append("created_at = if_not_exists(created_at, :created)")
    values = {":t": {"h": h, "exp": now + TICKET_TTL_S}, ":trial": now + TRIAL_S, ":created": utc_now_iso()}
    if auth.email:
        sets.append("email = if_not_exists(email, :e)")
        values[":e"] = auth.email
    key, expr = {"user_id": auth.user_id}, "SET " + ", ".join(sets)
    referrer = _referrer(body.ref if body else None, auth.user_id)
    if referrer:
        # Only the write that creates the account records the referrer: a retry or a concurrent second
        # first sign-in fails this condition and takes the plain write below, so a person counts once.
        try:
            await run_in_thread(
                _table().update_item,
                Key=key,
                UpdateExpression=expr + ", os_referred_by = :ref",
                ConditionExpression="attribute_not_exists(os_trial_ends_at)",
                ExpressionAttributeValues={**values, ":ref": referrer},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            referrer = None
    if referrer:
        try:
            await _credit(referrer)
        except Exception:  # the person is signed in either way; os_referred_by keeps the lost credit visible
            logger.exception("os referral credit failed: %s referred by %s", auth.user_id, referrer)
    else:
        await run_in_thread(_table().update_item, Key=key, UpdateExpression=expr, ExpressionAttributeValues=values)
    return TicketResponse(ticket=ticket)


@router.post("/session", response_model=SessionResponse, operation_id="os_create_session")
async def create_session(body: SessionRequest) -> SessionResponse:
    """Redeem a ticket for an app token. Unauthenticated: every refusal is the same 401, so the door says
    nothing about whether a person exists."""
    parts = _split(body.ticket)
    if not parts:
        raise HTTPException(status_code=401, detail="ticket_invalid")
    user_id, h = parts
    token, token_hash = _mint(user_id)
    try:
        # One conditional write consumes the ticket and stores the token: two redeems yield one token.
        row = await run_in_thread(
            _table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="REMOVE os_ticket ADD os_tokens :tok",
            ConditionExpression="os_ticket.h = :h AND os_ticket.exp > :now",
            ExpressionAttributeValues={":h": h, ":now": _now(), ":tok": {f"{time.time_ns()}:{token_hash}"}},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status_code=401, detail="ticket_invalid")
        raise
    stored = sorted(row["Attributes"]["os_tokens"])  # entries start with their mint time in ns
    if len(stored) > MAX_TOKENS:
        try:  # housekeeping: the person is signed in either way, and the next sign-in trims again
            await _drop_tokens(user_id, set(stored[:-MAX_TOKENS]))
        except Exception:
            logger.exception("os token trim failed for %s", user_id)
    return SessionResponse(token=token, accountId=user_id)


async def _drop_tokens(user_id: str, entries: set[str]) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"user_id": user_id},
        UpdateExpression="DELETE os_tokens :gone",
        ExpressionAttributeValues={":gone": entries},
    )


async def revoke_all(user_id: str) -> None:
    """Sign every Mac of a deleted person out. A person with no users row has nothing to revoke."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="REMOVE os_tokens, os_ticket",
            ConditionExpression="attribute_exists(user_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_os_account(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> tuple[dict, str]:
    """The users row an app token belongs to, and the token's stored entry; 401 for anything else."""
    parts = _split(creds.credentials) if creds else None
    if parts:
        user_id, h = parts
        got = await run_in_thread(_table().get_item, Key={"user_id": user_id}, ConsistentRead=True)
        row = got.get("Item") or {}
        for entry in row.get("os_tokens", ()):
            if hmac.compare_digest(entry.partition(":")[2], h):
                return row, entry
    raise HTTPException(status_code=401, detail="os_session_revoked")


@router.delete("/session", status_code=204, operation_id="os_delete_session")
async def delete_session(found: tuple[dict, str] = Depends(get_os_account)) -> Response:
    """Sign this app token out; the person's other tokens keep working."""
    row, entry = found
    await _drop_tokens(row["user_id"], {entry})
    return Response(status_code=204)


@router.get("/account", response_model=AccountResponse, operation_id="os_account")
async def get_account(found: tuple[dict, str] = Depends(get_os_account)) -> AccountResponse:
    row, _ = found
    ends = int(row["os_trial_ends_at"])
    return AccountResponse(
        accountId=row["user_id"],
        email=row.get("email") or "",
        name="",  # the backend stores no name for a person
        status="trial" if _now() < ends else "ended",
        trialEndsAt=ends,
        referralUrl=f"{settings.FRONTEND_URL.rstrip('/')}/os?ref={row['user_id'].removeprefix('user_')}",
        referralDaysEarned=int(row.get("os_referrals", 0)),  # one referral is one day
        referralCount=int(row.get("os_referrals", 0)),
    )
