import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_phase, launch_revenue as rev

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
# Same instants the pre-pivot suite used (the proration math below depends on
# NOW sitting 9d20h into a 30-day window) — only the field names moved.
CLOSES = datetime(2026, 9, 18, 16, tzinfo=timezone.utc)
S = launch_phase.Schedule(CLOSES, CLOSES + timedelta(hours=72), CLOSES + timedelta(hours=72, days=30), 200, None)


def ENTRY(owner_id: str, baseline_cents: int, **over) -> dict:
    """A settled entry: its baseline was computed after the window closed."""
    return {
        "owner_id": owner_id,
        "baseline_cents": baseline_cents,
        "baseline_at": (S.competition_start_at + timedelta(minutes=1)).isoformat(),
        **over,
    }


@pytest.fixture
def repo():
    with patch.object(rev, "launch_entries_repo") as r:
        r.set_revenue = AsyncMock()
        r.set_refresh_error = AsyncMock()
        r.set_key_connected = AsyncMock()
        yield r


@pytest.fixture(autouse=True)
def _reset_module_state():
    rev._inflight = None
    rev._last_started_at = None
    yield
    rev._inflight = None
    rev._last_started_at = None


async def test_refresh_entry_writes_window_minus_baseline(repo):
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk_test_x")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(12_000, "usd"))) as summer,
    ):
        await rev.refresh_entry(ENTRY("u1", 5_000), S, now=NOW)
    summer.assert_awaited_once_with("rk_test_x", S.competition_start_at, NOW)
    # NOW is 9d20h into a 30d window ⇒ 32.7…% of the 5_000 baseline has "happened".
    repo.set_revenue.assert_awaited_once_with(
        "u1", window_cents=12_000, delta_cents=12_000 - 1_639, currency="usd", baseline_prorated_cents=1_639
    )


async def test_refresh_entry_clamps_window_end_to_competition_end(repo):
    late = S.competition_end_at + timedelta(days=3)
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(0, "usd"))) as summer,
    ):
        await rev.refresh_entry(ENTRY("u1", 0), S, now=late)
    assert summer.await_args.args[2] == S.competition_end_at


async def test_refresh_entry_records_errors_instead_of_raising(repo):
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(side_effect=rev.KeyRejected("stripe_auth"))),
    ):
        await rev.refresh_entry(ENTRY("u1", 0), S, now=NOW)
    repo.set_refresh_error.assert_awaited_once_with("u1", "stripe_auth")
    with patch.object(rev, "load_key", AsyncMock(return_value=None)):
        await rev.refresh_entry(ENTRY("u2", 0), S, now=NOW)
    repo.set_refresh_error.assert_awaited_with("u2", "missing_key")


async def test_refresh_entry_swallows_repo_write_failures(repo):
    repo.set_revenue = AsyncMock(side_effect=RuntimeError("row gone"))
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(5, "usd"))),
    ):
        await rev.refresh_entry(ENTRY("u1", 0), S, now=NOW)  # no exception
    repo.set_refresh_error = AsyncMock(side_effect=RuntimeError("row gone"))
    with patch.object(rev, "load_key", AsyncMock(return_value=None)):
        await rev.refresh_entry(ENTRY("u2", 0), S, now=NOW)  # no exception


async def test_refresh_all_skips_fresh_rows_unless_forced(repo):
    fresh = (NOW - timedelta(minutes=10)).isoformat()
    stale = (NOW - timedelta(minutes=70)).isoformat()
    repo.list_all = AsyncMock(
        return_value=[
            {"owner_id": "a", "has_key": True, "refreshed_at": fresh},
            {"owner_id": "b", "has_key": True, "refreshed_at": stale},
            {"owner_id": "c", "has_key": True},
        ]
    )
    with (
        patch.object(rev, "refresh_entry", AsyncMock()) as one,
        patch.object(rev.launch_phase, "schedule", return_value=S),
    ):
        assert await rev.refresh_all(now=NOW) == 2
        assert {c.args[0]["owner_id"] for c in one.await_args_list} == {"b", "c"}
        one.reset_mock()
        assert await rev.refresh_all(force=True, now=NOW) == 3


async def test_refresh_all_skips_entries_without_has_key(repo):
    """Joined-but-never-connected entries have no Stripe key to read revenue
    with — the sweep must skip them entirely, not just leave them stale."""
    repo.list_all = AsyncMock(
        return_value=[
            {"owner_id": "a", "has_key": True},
            {"owner_id": "b", "has_key": False},
            {"owner_id": "c"},  # missing has_key entirely — also skipped
        ]
    )
    with (
        patch.object(rev, "refresh_entry", AsyncMock()) as one,
        patch.object(rev.launch_phase, "schedule", return_value=S),
    ):
        assert await rev.refresh_all(force=True, now=NOW) == 1
        assert {c.args[0]["owner_id"] for c in one.await_args_list} == {"a"}


async def test_refresh_all_noop_outside_window(repo):
    repo.list_all = AsyncMock(return_value=[{"owner_id": "a", "has_key": True}])
    with (
        patch.object(rev, "refresh_entry", AsyncMock()) as one,
        patch.object(rev.launch_phase, "schedule", return_value=S),
    ):
        assert await rev.refresh_all(now=CLOSES - timedelta(days=1)) == 0
        assert await rev.refresh_all(now=S.competition_end_at + timedelta(days=2)) == 0
        one.assert_not_awaited()


async def test_maybe_revalidate_schedules_once_and_throttles():
    with (
        patch.object(rev.launch_phase, "schedule", return_value=S),
        patch.object(rev, "refresh_all", AsyncMock(return_value=1)) as full,
    ):
        assert rev.maybe_revalidate(now=NOW) is True
        assert rev.maybe_revalidate(now=NOW) is False  # in flight
        await asyncio.sleep(0)
        await rev._inflight
        assert rev.maybe_revalidate(now=NOW + timedelta(minutes=1)) is False  # throttled
        assert rev.maybe_revalidate(now=NOW + timedelta(minutes=6)) is True
        await rev._inflight
        assert full.await_count == 2


async def test_maybe_revalidate_noop_when_unconfigured_or_outside_window():
    with patch.object(rev.launch_phase, "schedule", return_value=None):
        assert rev.maybe_revalidate(now=NOW) is False
    with patch.object(rev.launch_phase, "schedule", return_value=S):
        assert rev.maybe_revalidate(now=CLOSES - timedelta(days=1)) is False
        assert rev.maybe_revalidate(now=S.competition_end_at + timedelta(days=2)) is False
    assert rev._inflight is None


async def test_maybe_revalidate_clears_the_leaderboard_response_cache_on_completion():
    """A completed background sweep must invalidate routers.launch's own
    response cache, or a reader can see stale numbers for up to
    LEADERBOARD_TTL_S after the sweep already landed new ones."""
    from routers import launch as launch_router

    launch_router._RESP_CACHE.clear()
    factory = AsyncMock(side_effect=[{"n": 1}, {"n": 2}])
    try:
        first = await launch_router._cached("leaderboard", launch_router.LEADERBOARD_TTL_S, factory)
        assert first == {"n": 1}
        with (
            patch.object(rev.launch_phase, "schedule", return_value=S),
            patch.object(rev, "refresh_all", AsyncMock(return_value=0)),
        ):
            assert rev.maybe_revalidate(now=NOW) is True
            await rev._inflight
        second = await launch_router._cached("leaderboard", launch_router.LEADERBOARD_TTL_S, factory)
    finally:
        launch_router._RESP_CACHE.clear()
    assert second == {"n": 2}
    assert factory.await_count == 2


async def test_refresh_entry_finalises_a_baseline_computed_before_the_window_closed(repo):
    early = ENTRY("u1", 5_000, baseline_at=(S.competition_start_at - timedelta(hours=1)).isoformat())
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(side_effect=[(9_000, "usd"), (12_000, "usd")])) as summer,
    ):
        await rev.refresh_entry(early, S, now=NOW)
    assert summer.await_count == 2
    baseline_call, window_call = summer.await_args_list
    assert baseline_call.args[1] == S.competition_start_at - timedelta(days=rev.BASELINE_DAYS)
    assert baseline_call.args[2] == S.competition_start_at
    assert window_call.args[1] == S.competition_start_at and window_call.args[2] == NOW
    assert repo.set_key_connected.await_args.kwargs["baseline_cents"] == 9_000
    assert repo.set_key_connected.await_args.kwargs["currency"] == "usd"
    assert repo.set_key_connected.await_args.kwargs["baseline_at"]
    # the recomputed baseline, not the provisional 5_000, drives this pass's delta
    assert repo.set_revenue.await_args.kwargs["baseline_prorated_cents"] == 2_950  # 9_000 × 9d20h/30d
    assert repo.set_revenue.await_args.kwargs["delta_cents"] == 12_000 - 2_950


async def test_refresh_entry_finalises_when_baseline_at_is_missing(repo):
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(side_effect=[(100, "usd"), (400, "usd")])) as summer,
    ):
        await rev.refresh_entry({"owner_id": "u1", "baseline_cents": 0}, S, now=NOW)
    assert summer.await_count == 2
    repo.set_key_connected.assert_awaited_once()


async def test_refresh_entry_leaves_a_settled_baseline_alone(repo):
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(12_000, "usd"))) as summer,
    ):
        await rev.refresh_entry(ENTRY("u1", 5_000), S, now=NOW)
    assert summer.await_count == 1
    repo.set_key_connected.assert_not_awaited()


async def test_refresh_entry_errors_when_window_currency_differs_from_finalized_baseline(repo):
    """sum_net_revenue's own MixedCurrency only catches a mismatch WITHIN one
    page walk — a EUR baseline finalized on an earlier pass vs. a USD window
    read this pass (e.g. the entrant's Stripe account default currency
    changed) isn't caught there, so refresh_entry must check it itself."""
    entry = ENTRY("u1", 5_000, currency="eur")
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(12_000, "usd"))) as summer,
    ):
        await rev.refresh_entry(entry, S, now=NOW)
    assert summer.await_count == 1  # settled baseline: only the window is fetched
    repo.set_refresh_error.assert_awaited_once_with("u1", "mixed_currency")
    repo.set_revenue.assert_not_awaited()


# --- Prorated baseline -------------------------------------------------------


async def test_prorated_baseline_is_the_whole_baseline_at_the_finish(repo):
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(12_000, "usd"))),
    ):
        await rev.refresh_entry(ENTRY("u1", 5_000), S, now=S.competition_end_at)
    assert repo.set_revenue.await_args.kwargs["baseline_prorated_cents"] == 5_000
    assert repo.set_revenue.await_args.kwargs["delta_cents"] == 12_000 - 5_000


async def test_prorated_baseline_scales_with_elapsed_time(repo):
    three_days = S.competition_start_at + timedelta(days=3)  # 3 of 30 days
    with (
        patch.object(rev, "load_key", AsyncMock(return_value="rk")),
        patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(1_000, "usd"))),
    ):
        await rev.refresh_entry(ENTRY("u1", 5_000), S, now=three_days)
    assert repo.set_revenue.await_args.kwargs["baseline_prorated_cents"] == 500
    assert repo.set_revenue.await_args.kwargs["delta_cents"] == 1_000 - 500


def test_prorated_baseline_clamps_outside_the_window():
    assert rev.prorated_baseline(5_000, S, S.competition_start_at) == 0
    assert rev.prorated_baseline(5_000, S, S.competition_start_at - timedelta(days=1)) == 0
    assert rev.prorated_baseline(5_000, S, S.competition_end_at + timedelta(days=9)) == 5_000
