"""Tests for rollup_service.refresh_owner_day and update_service._rollup_pass.

All three sources (company_stripe_reads, owner_posthog, usage_repo) plus
rollup_repo are patched with patch.object — no DDB, no HTTP.
``asyncio_mode = "auto"`` (pyproject.toml) is in effect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call, patch

from core.services import rollup_service, update_service


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _patch_sources(money=None, traffic=None, usage=None, prior=None):
    """Patches for the three sources + rollup_repo. ``prior`` seeds the
    existing row get_days returns on the carry-forward path."""
    return (
        patch.object(rollup_service.company_stripe_reads, "day_money", AsyncMock(return_value=money)),
        patch.object(rollup_service.owner_posthog, "day_traffic", AsyncMock(return_value=traffic)),
        patch.object(rollup_service.usage_repo, "get_period_usage", AsyncMock(return_value=usage)),
        patch.object(rollup_service.rollup_repo, "get_days", AsyncMock(return_value=[prior] if prior else [])),
        patch.object(rollup_service.rollup_repo, "put_day", AsyncMock()),
    )


# ----------------------------------------------------------------------
# refresh_owner_day
# ----------------------------------------------------------------------


async def test_full_compose_writes_all_six_keys_without_prior_read():
    day = _today()  # current month, so the usage source is consulted
    money_p, traffic_p, usage_p, days_p, put_p = _patch_sources(
        money={"revenue_microcents": 5_000_000, "signups": 3},
        traffic={"visitors": 42},
        usage={"total_spend_microdollars": 123_456},
    )
    with money_p, traffic_p, usage_p as usage_mock, days_p as days_mock, put_p as put_mock:
        row = await rollup_service.refresh_owner_day("org_a", day)

    assert row == {
        "revenue_microcents": 5_000_000,
        "signups": 3,
        "visitors": 42,
        "followers": 0,
        "open_threads": 0,
        "agent_spend_microcents": 123_456,
    }
    put_mock.assert_awaited_once_with("org_a", day, row)
    usage_mock.assert_awaited_once_with("org_a", day[:7])
    days_mock.assert_not_awaited()  # no extra read on the happy path


async def test_source_outage_carries_forward_prior_row_values():
    """A None day_money keeps the prior row's revenue/signups — stale
    beats zero on a dashboard."""
    day = _today()
    money_p, traffic_p, usage_p, days_p, put_p = _patch_sources(
        money=None,
        traffic={"visitors": 7},
        usage={"total_spend_microdollars": 50},
        prior={"day": day, "revenue_microcents": 4_000_000, "signups": 2, "visitors": 99},
    )
    with money_p, traffic_p, usage_p, days_p as days_mock, put_p as put_mock:
        row = await rollup_service.refresh_owner_day("org_a", day)

    assert row["revenue_microcents"] == 4_000_000  # carried forward
    assert row["signups"] == 2  # carried forward
    assert row["visitors"] == 7  # fresh source wins over prior
    assert row["agent_spend_microcents"] == 50
    days_mock.assert_awaited_once_with("org_a", day, day)
    put_mock.assert_awaited_once()


async def test_outage_without_prior_row_falls_back_to_zeros():
    day = _today()
    money_p, traffic_p, usage_p, days_p, put_p = _patch_sources(
        money=None,
        traffic={"visitors": 7},
        usage=None,
    )
    with money_p, traffic_p, usage_p, days_p, put_p as put_mock:
        row = await rollup_service.refresh_owner_day("org_a", day)

    assert row["visitors"] == 7
    assert row["revenue_microcents"] == 0
    assert row["signups"] == 0
    assert row["agent_spend_microcents"] == 0
    put_mock.assert_awaited_once()


async def test_all_none_writes_nothing_and_returns_none():
    day = _today()
    money_p, traffic_p, usage_p, days_p, put_p = _patch_sources()
    with money_p, traffic_p, usage_p, days_p as days_mock, put_p as put_mock:
        assert await rollup_service.refresh_owner_day("org_a", day) is None
    put_mock.assert_not_awaited()
    days_mock.assert_not_awaited()


async def test_month_boundary_keeps_stamped_agent_spend():
    """A prior-month day never consults usage — its final month-to-date
    agent-spend stamp is carried forward, not zeroed."""
    money_p, traffic_p, usage_p, days_p, put_p = _patch_sources(
        money={"revenue_microcents": 100, "signups": 1},
        usage={"total_spend_microdollars": 555},  # must not be read
        prior={"day": "2020-01-15", "agent_spend_microcents": 999},
    )
    with money_p, traffic_p, usage_p as usage_mock, days_p, put_p as put_mock:
        row = await rollup_service.refresh_owner_day("org_a", "2020-01-15")

    usage_mock.assert_not_awaited()
    assert row["agent_spend_microcents"] == 999  # stamped value survives
    put_mock.assert_awaited_once()


# ----------------------------------------------------------------------
# update_service._rollup_pass
# ----------------------------------------------------------------------


async def test_rollup_pass_refreshes_today_and_yesterday_per_owner():
    refresh = AsyncMock(return_value={})
    owners = AsyncMock(return_value=["org_a", "org_b"])
    with (
        patch.object(update_service.billing_repo, "list_company_stripe_owners", owners),
        patch.object(rollup_service, "refresh_owner_day", refresh),
    ):
        await update_service._rollup_pass()

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    assert refresh.await_args_list == [
        call("org_a", today.isoformat()),
        call("org_a", yesterday.isoformat()),
        call("org_b", today.isoformat()),
        call("org_b", yesterday.isoformat()),
    ]


async def test_rollup_pass_one_owner_failure_does_not_stop_the_rest():
    refresh = AsyncMock(side_effect=[RuntimeError("stripe down"), {}, {}])
    owners = AsyncMock(return_value=["org_a", "org_b"])
    with (
        patch.object(update_service.billing_repo, "list_company_stripe_owners", owners),
        patch.object(rollup_service, "refresh_owner_day", refresh),
    ):
        await update_service._rollup_pass()  # must not raise

    # org_a's first day raised (skipping its second day); org_b got both days.
    assert refresh.await_count == 3
    assert [c.args[0] for c in refresh.await_args_list] == ["org_a", "org_b", "org_b"]
