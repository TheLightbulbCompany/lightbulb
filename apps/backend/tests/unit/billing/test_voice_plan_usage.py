from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from core.billing import voice_plan
from core.billing.metronome_client import MetronomeClient


def test_initial_and_cancelled_periods_use_exact_utc_contract_boundaries():
    account = {"voice_plan_started_at": "2026-11-08T00:00:00Z"}
    start, end = voice_plan.period(account, datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    assert start == datetime(2026, 11, 8, tzinfo=UTC) and end == datetime(2026, 12, 1, tzinfo=UTC)
    start, end = voice_plan.period(account, datetime(2026, 12, 1, 0, 1, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC) and end == datetime(2027, 1, 1, tzinfo=UTC)
    _, end = voice_plan.period(
        {**account, "voice_plan_ending_at": "2026-12-01T00:00:00Z"}, datetime(2026, 12, 5, tzinfo=UTC)
    )
    assert end == datetime(2026, 12, 1, tzinfo=UTC)


async def test_paid_usage_preserves_fractional_minutes_and_filters_one_enrollment(monkeypatch):
    monkeypatch.setattr(voice_plan.settings, "METRONOME_VOICE_METRIC_ID", "metric")
    client = MagicMock(scoped_voice_seconds=AsyncMock(return_value=61))
    account = {"metronome_customer_id": "mc", "voice_plan_usage_key": "exact-enrollment"}
    result = await voice_plan.usage(account, client=client)
    assert result["usedSeconds"] == 61 and result["used"] == 61 / 60 and result["timeZone"] == "UTC"
    args = client.scoped_voice_seconds.await_args.kwargs
    assert args["usage_key"] == "exact-enrollment" and args["customer_id"] == "mc"


async def test_provider_scoped_usage_paginates_without_dropping_fractional_aggregation():
    client = MetronomeClient(api_key="key", base_url="https://api.test/v1", http_client=MagicMock())
    client._post = AsyncMock(side_effect=[{"data": [{"value": 60}], "next_page": "next"}, {"data": [{"value": 1}]}])
    assert (
        await client.scoped_voice_seconds(
            customer_id="mc",
            metric_id="metric",
            usage_key="key",
            starting_on="2026-11-01T00:00:00Z",
            ending_before="2026-12-01T00:00:00Z",
        )
        == 61
    )
    assert client._post.await_args_list[0].args[1]["group_filters"] == {"voice_plan": ["key"]}
    assert client._post.await_args_list[1].args[0] == "/usage/groups?next_page=next"
