"""Finite Temporal rechecks for Metronome-backed voice allowance notices.

Workflow code stays deterministic and sandbox-safe. The Activity owns every
provider, DynamoDB, and SES interaction through the existing billing modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

RECHECK_DELAYS_SECONDS = (0, 15, 60, 300)


@dataclass(frozen=True)
class VoiceUsageNoticeInput:
    owner_id: str
    transaction_id: str
    contract_id: str
    usage_key: str
    period_start: str
    period_end: str


@workflow.defn
class VoiceUsageNoticeWorkflow:
    @workflow.run
    async def run(self, item: VoiceUsageNoticeInput) -> str:
        last_error: ActivityError | None = None
        for delay in RECHECK_DELAYS_SECONDS:
            if delay:
                await workflow.sleep(timedelta(seconds=delay))
            try:
                result = await workflow.execute_activity(
                    check_voice_usage_notice,
                    item,
                    start_to_close_timeout=timedelta(seconds=20),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError as exc:
                last_error = exc
                continue
            last_error = None
            if result not in {"below_threshold", "owner_email_unavailable", "provider_period_mismatch"}:
                return result
        if last_error:
            raise last_error
        return "rechecks_exhausted"


@activity.defn(name="check_voice_usage_notice")
async def check_voice_usage_notice(item: VoiceUsageNoticeInput) -> str:
    from core.billing.voice_usage_notices import check_and_notify

    return await check_and_notify(
        item.owner_id,
        contract_id=item.contract_id,
        usage_key=item.usage_key,
        period_start=item.period_start,
        period_end=item.period_end,
    )


WORKFLOWS = [VoiceUsageNoticeWorkflow]
ACTIVITIES = [check_voice_usage_notice]
