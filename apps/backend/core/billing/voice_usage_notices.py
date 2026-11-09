"""Transactional allowance notices for the explicitly paid voice plan.

Metronome remains the only usage source for a paid voice-plan notice. Callers
may retry this service after provider ingestion catches up; the frozen contract
period passed by the caller prevents a delayed check from drifting into a new
month or a replacement contract.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from core.auth import is_org_owner_id
from core.billing import voice_billing, voice_plan
from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.observability.metrics import put_metric
from core.repositories import billing_repo, messaging_repo
from core.services import email_service
from core.services import clerk_admin

logger = logging.getLogger(__name__)

THRESHOLDS = (80, 100)
_NOTICE_SK_PREFIX = "VOICEALLOWANCE#"


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("voice usage period timestamp must include a timezone")
    return parsed.astimezone(UTC)


def thresholds_reached(used_seconds: int) -> tuple[int, ...]:
    """Thresholds crossed at whole-second precision; 100% supersedes 80% in one check."""
    if used_seconds >= voice_billing.INCLUDED_MINUTES * 60:
        return (100,)
    if used_seconds >= voice_billing.INCLUDED_MINUTES * 60 * 80 // 100:
        return (80,)
    return ()


def _notice_sk(contract_id: str, period_start: datetime, threshold: int) -> str:
    return f"{_NOTICE_SK_PREFIX}{contract_id}#{period_start:%Y-%m-%dT%H:%M:%SZ}#{threshold}"


async def _claim(owner_id: str, *, contract_id: str, period_start: datetime, threshold: int, state: str) -> bool:
    """One immutable receipt per owner, contract, UTC period and threshold.

    ``sending`` is written before SES. If a timeout/crash leaves the result
    ambiguous, the receipt remains and a later Temporal retry cannot send a
    duplicate. ``covered`` marks the 80% receipt when a single delayed read
    first observes 100%.
    """
    item = {
        "pk": messaging_repo.owner_pk(owner_id),
        "sk": _notice_sk(contract_id, period_start, threshold),
        "status": state,
        "threshold": threshold,
        "contractId": contract_id,
        "periodStart": period_start.isoformat(),
        "createdAt": utc_now_iso(),
    }
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def _finish(
    owner_id: str,
    *,
    contract_id: str,
    period_start: datetime,
    threshold: int,
    status: str,
    message_id: str | None = None,
) -> None:
    names = {"#state": "status"}
    values = {":state": status, ":now": utc_now_iso()}
    update = "SET #state = :state, updatedAt = :now"
    if message_id:
        update += ", messageId = :message"
        values[":message"] = message_id
    await run_in_thread(
        messaging_repo._table().update_item,
        Key={"pk": messaging_repo.owner_pk(owner_id), "sk": _notice_sk(contract_id, period_start, threshold)},
        UpdateExpression=update,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


async def _owner_email(owner_id: str) -> str | None:
    """Resolve only the current admin who accepted the paid voice plan.

    Provisioning records and org creators can be stale or unrelated to billing.
    The accepted-by id is written by the authenticated admin-only enrollment
    flow, and Clerk membership is checked again at delivery time.
    """
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    accepted_by = account.get("voice_plan_accepted_by")
    if not isinstance(accepted_by, str) or not accepted_by:
        return None
    if is_org_owner_id(owner_id):
        members = await clerk_admin.list_organization_members(owner_id, strict=True)
        payer = next((member for member in members if member.get("user_id") == accepted_by), None)
        if not payer or payer.get("role") != "org:admin":
            return None
    elif accepted_by != owner_id:
        return None
    return (await clerk_admin.get_primary_email(accepted_by)).strip() or None


def _content(threshold: int, used_seconds: int) -> tuple[str, str, str]:
    used = used_seconds // 60
    included = voice_billing.INCLUDED_MINUTES
    rate = f"${voice_billing.OVERAGE_USD_PER_MINUTE:.2f}"
    url = f"{settings.FRONTEND_URL.rstrip('/')}/voice/settings"
    if threshold == 100:
        subject = "Your included voice minutes are used up"
        lead = f"Your voice assistant has used {used:,} of {included:,} included minutes this billing period."
        action = (
            f"Calls continue at {rate} per minute unless you selected “Send calls to my phone” in Voice settings. "
            "You can review that choice at any time."
        )
    else:
        subject = "Your voice plan is at 80% of its included minutes"
        lead = f"Your voice assistant has used {used:,} of {included:,} included minutes this billing period."
        action = (
            f"Additional voice time costs {rate} per minute after the included minutes. "
            "Calls keep answering unless you choose “Send calls to my phone” in Voice settings."
        )
    text = f"{lead}\n\n{action}\n\nReview Billing & minutes: {url}\n"
    e = html.escape
    body = (
        '<div style="font-family:sans-serif;max-width:560px">'
        f"<p>{e(lead)}</p><p>{e(action)}</p>"
        f'<p><a href="{e(url)}">Review Billing &amp; minutes</a></p></div>'
    )
    return subject, text, body


async def check_and_notify(
    owner_id: str,
    *,
    contract_id: str,
    usage_key: str,
    period_start: str,
    period_end: str,
) -> str:
    """Read one frozen paid-plan period and send its highest newly reached notice.

    The Temporal activity calls this on a bounded retry schedule. Provider
    errors raise so Temporal retries; a below-threshold provider result makes
    no claim; owner-email lookup is also before the send claim so it can retry.
    """
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if (
        not voice_billing.has_voice_plan(account)
        or account.get("metronome_voice_contract_id") != contract_id
        or account.get("voice_plan_usage_key") != usage_key
    ):
        return "inactive_or_changed_contract"

    frozen_start, frozen_end = _utc(period_start), _utc(period_end)
    if frozen_end <= datetime.now(UTC):
        return "expired_period"
    expected_start, expected_end = voice_plan.period(account, at=frozen_start)
    if expected_start.astimezone(UTC) != frozen_start or expected_end.astimezone(UTC) != frozen_end:
        return "changed_period"

    recipient = await _owner_email(owner_id)
    if not recipient:
        return "owner_email_unavailable"

    measured = await voice_plan.usage(account, at=frozen_start)
    if _utc(measured["periodStart"]) != frozen_start or _utc(measured["periodEnd"]) != frozen_end:
        return "provider_period_mismatch"
    used_seconds = int(measured["usedSeconds"])
    reached = thresholds_reached(used_seconds)
    if not reached:
        return "below_threshold"

    threshold = reached[-1]
    # If the first visible provider read jumps straight to 100%, suppress the
    # stale 80% warning. A concurrent 80% claim wins only if it began first.
    if threshold == 100:
        await _claim(owner_id, contract_id=contract_id, period_start=frozen_start, threshold=80, state="covered")
    if not await _claim(
        owner_id, contract_id=contract_id, period_start=frozen_start, threshold=threshold, state="sending"
    ):
        return "already_claimed"

    subject, text, body = _content(threshold, used_seconds)
    try:
        message_id = await email_service.send_email(
            to=recipient,
            subject=subject,
            html=body,
            text=text,
            from_addr=settings.EMAIL_FROM_WELCOME,
            kind="voice_allowance",
        )
    except Exception:
        logger.warning(
            "voice allowance email outcome unknown owner=%s threshold=%s", owner_id, threshold, exc_info=True
        )
        message_id = None

    status = "sent" if message_id else "unknown"
    await _finish(
        owner_id,
        contract_id=contract_id,
        period_start=frozen_start,
        threshold=threshold,
        status=status,
        message_id=message_id,
    )
    put_metric("voice.allowance_notice", dimensions={"threshold": str(threshold), "result": status})
    return status
