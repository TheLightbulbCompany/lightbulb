from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import messaging
from core.services.messaging import RunUsage, UsageReportRequest

USAGE = RunUsage(
    model="claude-sonnet-4-6",
    input_tokens=10,
    output_tokens=2,
    cache_read_tokens=0,
    cache_write_tokens=0,
    estimated_cost_usd=0.01,
)
BODY = UsageReportRequest(
    session_key="agent:friday:cron:job-1",
    agent_id="friday",
    run_id="run-1",
    success=True,
    duration_ms=1200,
    usage=USAGE,
)


def _patches(claimed=True):
    return (
        patch(
            "core.services.messaging.messaging_repo.claim_usage_receipt", new_callable=AsyncMock, return_value=claimed
        ),
        patch("core.services.messaging.record_usage", new_callable=AsyncMock),
        patch("core.services.messaging.run_analytics.schedule_run_completed", MagicMock(return_value=None)),
    )


@pytest.mark.asyncio
async def test_first_report_records_usage_and_analytics():
    claim, record, analytics = _patches()
    with claim as claim, record as record, analytics as analytics:
        response = await messaging.handle_usage_report("org_1", BODY)
    assert response.status == "applied"
    claim.assert_awaited_once_with("org_1", "run-1")
    record.assert_awaited_once_with(
        owner_id="org_1",
        user_id="org_1",
        model="claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=2,
        cache_read=0,
        cache_write=0,
        raw_cost_microcents=10_000,
        room_turn=False,
        event_kind=None,
    )
    kwargs = analytics.call_args.kwargs
    assert (kwargs["owner_id"], kwargs["phase"], kwargs["payload"]["runId"], kwargs["parsed"]["source"]) == (
        "org_1",
        "end",
        "run-1",
        "cron",
    )
    assert kwargs["payload"]["data"]["durationMs"] == 1200


@pytest.mark.asyncio
async def test_replay_and_room_sessions_write_nothing():
    claim, record, analytics = _patches(claimed=False)
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", BODY)).status == "replayed"
        room = BODY.model_copy(
            update={
                "session_key": "agent:11111111-1111-4111-8111-111111111111:lightbulb:channel:"
                "22222222-2222-4222-8222-222222222222"
            }
        )
        assert (await messaging.handle_usage_report("org_1", room)).status == "ignored"
    record.assert_not_awaited()
    analytics.assert_not_called()


@pytest.mark.asyncio
async def test_zero_usage_skips_counters_but_still_emits_analytics():
    claim, record, analytics = _patches()
    empty = BODY.model_copy(
        update={
            "usage": RunUsage(
                model="unknown",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                estimated_cost_usd=None,
            ),
            "success": False,
        }
    )
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", empty)).status == "applied"
    record.assert_not_awaited()
    assert analytics.call_args.kwargs["phase"] == "error"
