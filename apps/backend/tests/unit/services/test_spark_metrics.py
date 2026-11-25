"""Spark admin metrics: derive() over canned HogQL rows, compute() with posthog_admin.hogql mocked."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import spark_metrics as sm  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)  # Wednesday; week = 2026-11-23


def _ok(results):
    return {"columns": [], "results": results, "stubbed": False, "error": None}


@pytest.fixture(autouse=True)
def _no_cache():
    sm._cache = None
    yield
    sm._cache = None


def test_internal_filter_excludes_configured_ids_and_flag():
    with (
        patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "user_a, user_b"),
        patch.object(sm.settings, "PLATFORM_ADMIN_USER_IDS", "user_admin"),
    ):
        f = sm._internal_filter()
    assert "is_internal" in f
    for uid in ("user_a", "user_b", "user_admin"):
        assert f"'{uid}'" in f


def test_internal_filter_drops_unsafe_ids():
    with patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "ok_1,x') or 1=1 --"):
        f = sm._internal_filter()
    assert "'ok_1'" in f and "1=1" not in f


def test_every_query_carries_the_filters():
    for name, q in sm.build_queries().items():
        assert "is_internal" in q, name


def test_derive_maps_rows_and_handles_unknown_platform_and_immature_cohorts():
    rows = {
        "doers_weekly": [["2026-11-16", 4], ["2026-11-23", 2]],
        "doers_7d": [[3]],
        "activation": [
            ["2026-09-28", "mac", 10, 6],
            ["2026-09-28", "unknown", 5, 1],
            ["2026-11-23", "windows", 4, 2],
        ],
        "retention": [["2026-09-28", 3]],
        "tasks_weekly": [["2026-11-23", "task_completed", 9], ["2026-11-23", "task_failed", 1]],
        "failed_reasons": [["auth", 5], ["quota", 2]],
        "problem_kinds": [],
        "funnel": [["app_first_launch", 20], ["task_completed", 12]],
        "unhealthy": [[2]],
    }
    d = sm.derive(rows, NOW)
    assert len(d["weeks"]) == 12 and d["weeks"][-1] == "2026-11-23"
    assert d["wad"]["rolling_7d"] == 3
    assert [w["doers"] for w in d["wad"]["weekly"]][-2:] == [4, 2]
    old = next(c for c in d["cohorts"] if c["week"] == "2026-09-28")
    assert old["installs"] == 15 and old["activated"] == 7
    assert old["activation_rate"] == pytest.approx(7 / 15)
    assert old["w4_retained"] == 3 and old["w4_retention"] == pytest.approx(3 / 15)
    assert old["activated_by_platform"] == {"mac": 6, "unknown": 1, "windows": 0}
    cur = next(c for c in d["cohorts"] if c["week"] == "2026-11-23")
    assert cur["w4_retention"] is None  # too young for a day-28 reading
    empty = next(c for c in d["cohorts"] if c["week"] == "2026-10-05")
    assert empty["activation_rate"] is None  # no installs: not a fake 0%
    assert d["task_success"]["weekly"][-1]["rate"] == pytest.approx(0.9)
    assert d["task_success"]["top_failed_reasons"][0] == {"key": "auth", "count": 5}
    assert [s["users"] for s in d["funnel"]] == [0, 20, 0, 0, 12, 0]
    assert d["reliability"]["unhealthy_installs_24h"] == 2


@pytest.mark.asyncio
async def test_compute_stubbed_when_posthog_unconfigured():
    stub = {"columns": [], "results": [], "stubbed": True, "error": None}
    with patch("core.services.posthog_admin.hogql", AsyncMock(return_value=stub)):
        data, state, errors = await sm.compute()
    assert data is None and state == "unavailable" and errors[0]["reason"] == "not configured"


@pytest.mark.asyncio
async def test_compute_empty_results_render_zeros_and_cache():
    mock = AsyncMock(return_value=_ok([]))
    with patch("core.services.posthog_admin.hogql", mock):
        data, state, _ = await sm.compute()
        calls = mock.await_count
        await sm.compute()
    assert state == "ok" and data["wad"]["rolling_7d"] == 0 and data["reliability"]["unhealthy_installs_24h"] == 0
    assert mock.await_count == calls  # second read served from cache


@pytest.mark.asyncio
async def test_compute_partial_on_one_failed_query_and_not_cached():
    async def fake(q, **_):
        if "problem_raised" in q:
            return {"columns": [], "results": [], "stubbed": False, "error": "timeout"}
        return _ok([])

    with patch("core.services.posthog_admin.hogql", AsyncMock(side_effect=fake)):
        data, state, errors = await sm.compute()
    assert state == "partial" and data["failed_queries"] == ["problem_kinds"] and errors[0]["reason"] == "timeout"
    assert sm._cache is None
