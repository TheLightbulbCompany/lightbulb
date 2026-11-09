"""The paid allowance check catches delayed Metronome visibility without a settings-page poll."""

import uuid

import pytest
from temporalio import activity
from temporalio.worker import Worker

from core.temporal.voice_usage_notices import VoiceUsageNoticeInput, VoiceUsageNoticeWorkflow


@pytest.mark.asyncio
async def test_last_call_rechecks_frozen_period_until_metronome_usage_is_visible(temporal_env):
    seen = []
    results = ["below_threshold", "below_threshold", "sent"]

    @activity.defn(name="check_voice_usage_notice")
    async def check(item: VoiceUsageNoticeInput) -> str:
        seen.append(item)
        return results.pop(0)

    queue = f"voice-usage-{uuid.uuid4()}"
    item = VoiceUsageNoticeInput(
        owner_id="org-1",
        transaction_id="call-1",
        contract_id="contract-1",
        usage_key="usage-1",
        period_start="2026-11-01T00:00:00+00:00",
        period_end="2026-12-01T00:00:00+00:00",
    )
    async with Worker(
        temporal_env.client,
        task_queue=queue,
        workflows=[VoiceUsageNoticeWorkflow],
        activities=[check],
    ):
        handle = await temporal_env.client.start_workflow(
            "VoiceUsageNoticeWorkflow", item, id=f"voice-usage-notice-test-{uuid.uuid4()}", task_queue=queue
        )
        assert await handle.result() == "sent"

    assert len(seen) == 3
    assert all(check_input == item for check_input in seen)


@pytest.mark.asyncio
async def test_transient_query_error_is_rechecked_and_eventually_sends(temporal_env):
    calls = 0

    @activity.defn(name="check_voice_usage_notice")
    async def check(_item: VoiceUsageNoticeInput) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Metronome read timed out")
        return "unknown"

    queue = f"voice-usage-{uuid.uuid4()}"
    item = VoiceUsageNoticeInput(
        "org-1", "call-2", "contract-1", "usage-1", "2026-11-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"
    )
    async with Worker(
        temporal_env.client,
        task_queue=queue,
        workflows=[VoiceUsageNoticeWorkflow],
        activities=[check],
    ):
        handle = await temporal_env.client.start_workflow(
            "VoiceUsageNoticeWorkflow", item, id=f"voice-usage-notice-test-{uuid.uuid4()}", task_queue=queue
        )
        assert await handle.result() == "unknown"
    assert calls == 2
