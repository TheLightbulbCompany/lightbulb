from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing import voice_plan
from core.billing.metronome_client import MetronomeClient


def _d(y, m, d, *rest):
    return datetime(y, m, d, *rest, tzinfo=UTC)


def _anchored(start: str) -> dict:
    return {"voice_plan_started_at": start, "voice_plan_billing_anchor": start}


@pytest.mark.parametrize(
    ("anchor", "at", "start", "end"),
    [
        # Day 1, 15 and 19: the same day every month.
        ("2026-11-01T00:00:00.000Z", _d(2026, 11, 30, 23, 59), _d(2026, 11, 1), _d(2026, 12, 1)),
        ("2026-01-15T00:00:00.000Z", _d(2026, 6, 14, 23, 59, 59), _d(2026, 5, 15), _d(2026, 6, 15)),
        ("2026-01-15T00:00:00.000Z", _d(2026, 6, 15), _d(2026, 6, 15), _d(2026, 7, 15)),
        # The 19 Nov sandbox run: Stripe's 19 Nov - 19 Dec, not 19 Nov - 1 Dec.
        ("2026-11-19T00:00:00.000Z", _d(2026, 11, 19, 15), _d(2026, 11, 19), _d(2026, 12, 19)),
        ("2026-11-19T00:00:00.000Z", _d(2026, 12, 18, 23, 59, 59, 999999), _d(2026, 11, 19), _d(2026, 12, 19)),
        ("2026-11-19T00:00:00.000Z", _d(2026, 12, 19), _d(2026, 12, 19), _d(2027, 1, 19)),
        # December to January, across the year end.
        ("2026-11-19T00:00:00.000Z", _d(2027, 1, 18, 23), _d(2026, 12, 19), _d(2027, 1, 19)),
        ("2026-12-31T00:00:00.000Z", _d(2027, 1, 30), _d(2026, 12, 31), _d(2027, 1, 31)),
        # Day 31 falls back to a shorter month's last day, then returns to the 31st.
        ("2026-12-31T00:00:00.000Z", _d(2027, 2, 27), _d(2027, 1, 31), _d(2027, 2, 28)),
        ("2026-12-31T00:00:00.000Z", _d(2027, 2, 28), _d(2027, 2, 28), _d(2027, 3, 31)),
        ("2026-12-31T00:00:00.000Z", _d(2027, 4, 30, 12), _d(2027, 4, 30), _d(2027, 5, 31)),
        ("2027-01-31T00:00:00.000Z", _d(2028, 2, 28, 23), _d(2028, 1, 31), _d(2028, 2, 29)),  # leap year
        ("2027-01-31T00:00:00.000Z", _d(2028, 2, 29), _d(2028, 2, 29), _d(2028, 3, 31)),
        # Day 30: February is shorter (non-leap and leap); March is back on the 30th.
        ("2027-01-30T00:00:00.000Z", _d(2027, 2, 28), _d(2027, 2, 28), _d(2027, 3, 30)),
        ("2027-01-30T00:00:00.000Z", _d(2028, 2, 29), _d(2028, 2, 29), _d(2028, 3, 30)),
        ("2027-01-30T00:00:00.000Z", _d(2028, 2, 28), _d(2028, 1, 30), _d(2028, 2, 29)),
        # Day 29: only a non-leap February is shorter.
        ("2027-01-29T00:00:00.000Z", _d(2027, 2, 28), _d(2027, 2, 28), _d(2027, 3, 29)),
        ("2027-01-29T00:00:00.000Z", _d(2027, 2, 27), _d(2027, 1, 29), _d(2027, 2, 28)),
        ("2027-01-29T00:00:00.000Z", _d(2028, 2, 29), _d(2028, 2, 29), _d(2028, 3, 29)),
        ("2027-01-29T00:00:00.000Z", _d(2028, 2, 28, 23), _d(2028, 1, 29), _d(2028, 2, 29)),
    ],
)
def test_period_follows_the_contract_start_day(anchor, at, start, end):
    assert voice_plan.period(_anchored(anchor), at) == (start, end)


def test_first_period_starts_on_the_contract_and_a_cancelled_plan_ends_on_its_end_date():
    account = _anchored("2026-11-19T00:00:00.000Z")
    assert voice_plan.period(account, _d(2026, 11, 19)) == (_d(2026, 11, 19), _d(2026, 12, 19))
    cancelled = {**account, "voice_plan_ending_at": "2027-01-19T00:00:00+00:00"}
    assert voice_plan.period(cancelled, _d(2027, 1, 18, 23)) == (_d(2026, 12, 19), _d(2027, 1, 19))
    # After the end, the last paid window (cut at the end date) is the one reported.
    assert voice_plan.period(cancelled, _d(2027, 3, 2)) == (_d(2026, 12, 19), _d(2027, 1, 19))
    mid = {**account, "voice_plan_ending_at": "2027-01-05T10:00:00+00:00"}
    assert voice_plan.period(mid, _d(2027, 2, 1)) == (_d(2026, 12, 19), _d(2027, 1, 5, 10))


def test_the_window_follows_the_anchor_metronome_reports_not_the_start_day():
    # A contract on Metronome's default schedule reports the 1st, whatever day it started.
    account = {
        "voice_plan_started_at": "2026-11-08T00:00:00Z",
        "voice_plan_billing_anchor": "2026-11-01T00:00:00+00:00",
    }
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
    account = {
        "metronome_customer_id": "mc",
        "voice_plan_usage_key": "exact-enrollment",
        "voice_plan_billing_anchor": "2026-11-19T00:00:00+00:00",
    }
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


async def test_enrollment_stores_the_anchor_metronome_reports():
    client = MagicMock(
        list_contracts=AsyncMock(return_value=[]),
        create_voice_contract=AsyncMock(return_value="vc"),
        get_contract=AsyncMock(
            return_value={
                "id": "vc",
                "starting_at": "2026-11-19T00:00:00.000Z",
                "usage_statement_schedule": {"frequency": "MONTHLY", "billing_anchor_date": "2026-11-19T00:00:00.000Z"},
            }
        ),
    )
    with (
        patch.object(voice_plan.metronome_provisioning, "_maybe_link_stripe", AsyncMock(return_value=True)),
        patch.object(voice_plan.billing_repo, "set_voice_plan", AsyncMock()) as store,
    ):
        await voice_plan._enroll("org_1", {"metronome_customer_id": "mc"}, client)
    assert store.await_args.kwargs["billing_anchor"] == "2026-11-19T00:00:00.000Z"
    assert store.await_args.kwargs["started_at"] == "2026-11-19T00:00:00.000Z"


async def test_enrollment_refuses_a_contract_without_a_monthly_anchor():
    client = MagicMock(
        list_contracts=AsyncMock(return_value=[{"id": "vc", "uniqueness_key": "lightbulb-voice:mc:voice-agents-v1"}]),
        get_contract=AsyncMock(return_value={"id": "vc", "starting_at": "2026-11-19T00:00:00.000Z"}),
    )
    with (
        patch.object(voice_plan.metronome_provisioning, "_maybe_link_stripe", AsyncMock(return_value=True)),
        patch.object(voice_plan.billing_repo, "set_voice_plan", AsyncMock()) as store,
        pytest.raises(voice_plan.VoicePlanError, match="voice_contract_incomplete"),
    ):
        await voice_plan._enroll("org_1", {"metronome_customer_id": "mc"}, client)
    store.assert_not_awaited()
