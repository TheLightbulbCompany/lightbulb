"""Customer lifecycle audience assembly.

This module intentionally returns an allowlisted contract: raw Clerk, container,
and billing rows may contain credentials and provider identifiers.
"""

import asyncio

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import lifecycle_ledger, user_repo
from core.services import admin_service

_INTERNAL_DOMAINS = {"thelightbulb.company"}
_ACTIVE_SUBSCRIPTIONS = {"active", "trialing", "trial_local", "past_due"}


async def _raw_candidates(*, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    page = await admin_service.list_users(limit=limit, cursor=cursor)
    return page["users"], page.get("cursor")


def _internal(email: str) -> bool:
    _, _, domain = email.lower().rpartition("@")
    return domain in _INTERNAL_DOMAINS


async def _is_ses_suppressed(email: str) -> bool:
    """Return true for SES account-level suppression; fail closed on AWS errors."""
    client = boto3.client("sesv2", region_name=settings.AWS_REGION)

    def read() -> bool:
        try:
            client.get_suppressed_destination(EmailAddress=email)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NotFoundException":
                return False
            return True

    return await asyncio.to_thread(read)


async def build_candidates(*, audience: str, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    rows, next_cursor = await _raw_candidates(limit=limit, cursor=cursor)
    result: list[dict] = []
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email or _internal(email):
            continue
        user_id = row["customer_id"]
        preferences, suppressed, ledger_state = await asyncio.gather(
            user_repo.get(user_id),
            _is_ses_suppressed(email),
            lifecycle_ledger.get_recipient_state(email),
        )
        preferences = preferences or {}
        unsubscribed_at = preferences.get("marketing_email_unsubscribed_at") or row.get("marketing_unsubscribed_at")
        consent_at = preferences.get("marketing_email_consent_at") or row.get("marketing_consent_at")
        account_active = row.get("account_status") == "active"
        transactional = account_active and not suppressed
        marketing = transactional and bool(consent_at) and not unsubscribed_at
        eligibility = {
            "transactional": transactional,
            "marketing": marketing,
            "ses_suppressed": suppressed,
            "marketing_consented": bool(consent_at),
            "marketing_unsubscribed": bool(unsubscribed_at),
            "lifecycle_frequency_cap_hours": 72,
            "last_lifecycle_sent_at": (ledger_state or {}).get("last_lifecycle_sent_at"),
            "last_monthly_broadcast_at": (ledger_state or {}).get("last_monthly_broadcast_at"),
        }
        if audience == "marketing" and not marketing:
            continue
        if audience == "transactional" and not transactional:
            continue
        result.append(
            {
                key: row.get(key)
                for key in (
                    "customer_id",
                    "workspace_id",
                    "email",
                    "first_name",
                    "signup_at",
                    "last_sign_in_at",
                    "last_activity_at",
                    "activation",
                    "plan",
                    "trial_ends_at",
                    "subscription_status",
                    "account_status",
                )
            }
        )
        result[-1]["eligibility"] = eligibility
    return result, next_cursor
