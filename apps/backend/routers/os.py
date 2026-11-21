"""Spark (the Mac app) account and referrals, keyed on the person, never an organisation.

The app signs in with Clerk's own SDK and sends its Clerk session token, like the web app. A person's Spark
account is a few attributes on their users row: `os_trial_ends_at`, `os_created_at`, `os_referrals` (people
they referred) and `os_referred_by`."""

import logging
import re
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_human_session
from core.config import settings
from core.dynamodb import get_table, is_cancelled, run_in_thread, transact, utc_now_iso

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


@router.get("/account", response_model=AccountResponse, operation_id="os_account")
async def get_account(auth: AuthContext = Depends(get_human_session)) -> AccountResponse:
    """The signed-in person's Spark account. The first read creates it (trial ending three days from now) in
    the same single write every read makes: `if_not_exists` makes concurrent first reads agree, and a later
    read never moves the trial."""
    now = _now()
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
    row = written["Attributes"]
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
