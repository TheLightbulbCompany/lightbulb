"""Voice-call billing primitives: who pays, how long a call may run, what a second costs.

Credits mode mirrors the openrouter-settlement Lambda: provider cost is ingested to Metronome
and priced there at the managed-credit markup; the fast-gate row gets the same provisional
debit settlement applies (the next balance refresh overwrites it from Metronome). Free mode is
a per-workspace daily seconds counter in the containers table (``__voice__#<owner>``).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from core.billing import metronome_client
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.repositories import billing_repo, messaging_repo
from core.services import metronome_gate, trial_cap
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
FREE_SECONDS_PER_DAY = 600
MAX_CALL_SECONDS = 1800
MIN_CALL_SECONDS = 60
FREE_KEY_PREFIX = "__voice__#"
VOICE_PROVIDER_USD_PER_MINUTE = Decimal("0.05")
_NANODOLLARS_PER_USD = Decimal(10**9)
_LIVE_STATUSES = {"active", "trialing", "trial_local"}
#: The voice-agents plan: $250 a month per business with these minutes, then this much a minute.
INCLUDED_MINUTES = 1500
OVERAGE_USD_PER_MINUTE = 0.35


def _containers():
    return get_table("containers")


def has_voice_plan(account: dict | None) -> bool:
    if not account or not account.get("metronome_voice_contract_id") or account.get("voice_plan_status") == "canceled":
        return False
    # The $250 flat fee is a Stripe subscription: active/trialing (a 100%-off coupon is active) is the entitlement.
    if account.get("voice_plan_stripe_status") not in ("active", "trialing"):
        return False
    now = datetime.now(UTC)
    ending_at = account.get("voice_plan_ending_at")
    return not ending_at or datetime.fromisoformat(ending_at) > now


def billing_mode(account: dict | None) -> Literal["credits", "free", "plan"] | None:
    """None = no voice (no row, lapsed, expired trial). Stored fields only — never
    provider_choice_from_row, which reports openrouter_managed for trial_local."""
    if has_voice_plan(account):
        return "plan"
    if account and account.get("metronome_voice_contract_id"):
        return None  # a pending/failed voice invoice cannot fall back to legacy free/credit voice
    if not account or account.get("subscription_status") not in _LIVE_STATUSES:
        return None
    if account.get("subscription_status") == "trial_local" and trial_cap.is_trial_expired(account):
        return None
    if account.get("provider_choice") == "openrouter_managed" and not trial_cap.is_managed_capped(account):
        return "credits"
    return "free"


def provider_nanodollars(seconds: int) -> int:
    return int(
        (Decimal(seconds) * VOICE_PROVIDER_USD_PER_MINUTE * _NANODOLLARS_PER_USD / 60).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def charge_microcents(seconds: int) -> int:
    """Same formula as openrouter-settlement ``debit_microcents``: cost × markup, rounded up."""
    markup = Decimal(settings.OPENROUTER_MARKUP or "0")
    if not markup.is_finite() or markup < 1:
        raise ValueError("OPENROUTER_MARKUP must be a decimal >= 1")
    return int((Decimal(provider_nanodollars(seconds)) * markup / 1000).to_integral_value(rounding=ROUND_CEILING))


async def budget_seconds(owner_id: str, mode: str) -> int:
    if mode == "free":
        item = (
            await run_in_thread(
                _containers().get_item, Key={"owner_id": f"{FREE_KEY_PREFIX}{owner_id}"}, ConsistentRead=True
            )
        ).get("Item") or {}
        used = int(item.get("usedSeconds", 0)) if item.get("day") == trial_cap.utc_today() else 0
        return max(0, min(FREE_SECONDS_PER_DAY - used, MAX_CALL_SECONDS))
    return (await credit_snapshot(owner_id))[0]


async def credit_snapshot(owner_id: str) -> tuple[int, dict]:
    """(budget seconds, the fast-gate row the budget was computed from). The row is what a
    compare-and-set hold must still find unchanged; {} when there is nothing to hold against."""
    if await metronome_gate.is_owner_blocked(owner_id):
        return 0, {}
    row = await MetronomeFastGate().get_row(owner_id)
    if not row or row.get("remaining_microcents") is None:
        return 0, {}
    headroom = (
        int(row["remaining_microcents"])
        - int(row.get("reserved_microcents", 0))
        - int(row.get("openrouter_reserved_microcents", 0))
        - metronome_gate.RESERVE_ESTIMATE_MICROCENTS
    )
    per_minute = charge_microcents(60)
    return max(0, min(math.floor(headroom * 60 / per_minute), MAX_CALL_SECONDS)), row


def month_start(tz_name: str | None, at: datetime | None = None) -> datetime:
    """The 1st of ``at``'s month (default now), midnight in the business's timezone (New York when unset or bad)."""
    try:
        tz = ZoneInfo(tz_name or "America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("America/New_York")
    return (at or datetime.now(UTC)).astimezone(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _month_key(owner_id: str, start: datetime) -> dict:
    """``OWNER#<owner> VOICEMINUTES#YYYY-MM``: the month's call and meeting seconds, added at each end."""
    return {"pk": messaging_repo.owner_pk(owner_id), "sk": f"VOICEMINUTES#{start:%Y-%m}"}


async def add_month_seconds(owner_id: str, seconds: int, at: datetime) -> None:
    """Add a finished call's or meeting's seconds to the month it started in (the business's timezone)."""
    if seconds <= 0:
        return
    tz = (await messaging_repo.get_voice_agent(owner_id) or {}).get("timezone")
    await run_in_thread(
        messaging_repo._table().update_item, Key=_month_key(owner_id, month_start(tz, at)),
        UpdateExpression="ADD #s :s", ExpressionAttributeNames={"#s": "seconds"}, ExpressionAttributeValues={":s": seconds},
    )  # fmt: skip


async def counted_seconds(owner_id: str, tz_name: str | None) -> int | None:
    """This month's counter, or None before its first call or meeting ends."""
    row = await messaging_repo._get(**_month_key(owner_id, month_start(tz_name)))
    return int(row["seconds"]) if row else None


async def report_minutes(owner_id: str, transaction_id: str, started_at: str, seconds: int, kind: str) -> None:
    """Persist one immutable Metronome event and enqueue durable delivery.

    Metronome's transaction id dedupes replays. The messaging stream/Pipe/FIFO
    outbox delivers events after the voice lifecycle callback returns, so a
    provider or Temporal outage cannot discard its only scheduling attempt.
    """
    if seconds <= 0:
        return
    event_at = datetime.fromisoformat(started_at).astimezone(UTC)
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    properties = {"seconds": str(seconds), "kind": kind}
    event = {
        "transactionId": transaction_id,
        "customerId": owner_id,
        "timestamp": event_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": seconds,
        "kind": kind,
        "properties": properties,
    }
    # A late end callback still belongs to the paid occurrence that started before cancellation.
    if (
        account.get("metronome_voice_contract_id")
        and account.get("voice_plan_usage_key")
        and (
            not account.get("voice_plan_started_at")
            or event_at >= datetime.fromisoformat(account["voice_plan_started_at"])
        )
        and (
            not account.get("voice_plan_ending_at")
            or event_at < datetime.fromisoformat(account["voice_plan_ending_at"])
        )
    ):
        from core.billing import voice_plan

        period_start, period_end = voice_plan.period(account, at=event_at)
        properties["voice_plan"] = account["voice_plan_usage_key"]
        event.update(
            {
                "contractId": account["metronome_voice_contract_id"],
                "usageKey": account["voice_plan_usage_key"],
                "periodStart": period_start.astimezone(UTC).isoformat(),
                "periodEnd": period_end.astimezone(UTC).isoformat(),
            }
        )
    await messaging_repo.put_voice_usage_event(owner_id, transaction_id, event)


async def deliver_usage_event(owner_id: str, transaction_id: str) -> str:
    """Retry-safe worker for the durable voice usage outbox pointer."""
    key = {"pk": messaging_repo.owner_pk(owner_id), "sk": messaging_repo.voice_usage_sk(transaction_id)}
    row = await messaging_repo._get(key["pk"], key["sk"])
    event = (row or {}).get("voiceUsageEvent")
    if not row or row.get("ownerId") != owner_id or not isinstance(event, dict):
        raise RuntimeError("voice usage outbox has no matching immutable event")
    if event.get("transactionId") != transaction_id or event.get("customerId") != owner_id:
        raise RuntimeError("voice usage outbox identity does not match its frozen event")

    if not row.get("metronomeIngestedAt"):
        if not settings.METRONOME_API_KEY:
            raise RuntimeError("voice usage delivery requires METRONOME_API_KEY")
        provider_event = {
            "transaction_id": transaction_id,
            "customer_id": owner_id,
            "event_type": "voice_minutes",
            "timestamp": event["timestamp"],
            "properties": event["properties"],
        }
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
            await metronome_client.for_settings(http).ingest([provider_event])
        await messaging_repo.mark_voice_usage_ingested(owner_id, transaction_id)
        from core.observability.metrics import put_metric

        put_metric("voice.usage_delivery", dimensions={"step": "metronome", "result": "accepted"})

    if event.get("contractId"):
        from core.temporal.client import start_voice_usage_notice
        from core.temporal.voice_usage_notices import VoiceUsageNoticeInput

        await start_voice_usage_notice(
            VoiceUsageNoticeInput(
                owner_id=owner_id,
                transaction_id=transaction_id,
                contract_id=event["contractId"],
                usage_key=event["usageKey"],
                period_start=event["periodStart"],
                period_end=event["periodEnd"],
            )
        )
        from core.observability.metrics import put_metric

        put_metric("voice.usage_delivery", dimensions={"step": "notice_workflow", "result": "started"})
    return "delivered"


async def report_owner_minutes(owner_id: str, transaction_id: str, started_at: str, seconds: int) -> None:
    """Freeze one room/job receipt before ingest; retries reuse its original start and provider seconds."""
    from core.services import voice_calls

    key = {"pk": messaging_repo.owner_pk(owner_id), "sk": f"VOICEUSAGE#{transaction_id}"}
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    # Scope plan usage to the epoch that began on the voice contract; never stack a credit debit.
    at = datetime.fromisoformat(started_at)
    on_plan = bool(
        account.get("metronome_voice_contract_id")
        and (not account.get("voice_plan_started_at") or at >= datetime.fromisoformat(account["voice_plan_started_at"]))
        and (not account.get("voice_plan_ending_at") or at < datetime.fromisoformat(account["voice_plan_ending_at"]))
    )
    mode = (
        "plan"
        if on_plan
        else (
            "credits"
            if account.get("provider_choice") == "openrouter_managed" and not trial_cap.is_managed_capped(account)
            else "free"
        )
    )
    row = {**key, "startedAt": started_at, "seconds": seconds, "billing": mode}
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=row, ConditionExpression="attribute_not_exists(sk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = (await run_in_thread(messaging_repo._table().get_item, Key=key, ConsistentRead=True))["Item"]
    await report_minutes(owner_id, transaction_id, row["startedAt"], int(row["seconds"]), "owner")
    if row.get("billingSettled"):
        return
    try:
        await voice_calls._bill(
            {
                "ownerId": owner_id,
                "callId": transaction_id,
                "startedAt": int(datetime.fromisoformat(row["startedAt"]).timestamp()),
                "billing": row.get("billing", mode),
                "holdMicrocents": 0,
                "settlementReceipt": key,
            },
            int(row["seconds"]),
        )
    except ClientError:
        # Concurrent/retried ends may both ingest, but only one atomic settlement can debit.
        settled = (await run_in_thread(messaging_repo._table().get_item, Key=key, ConsistentRead=True)).get(
            "Item"
        ) or {}
        if not settled.get("billingSettled"):
            raise
