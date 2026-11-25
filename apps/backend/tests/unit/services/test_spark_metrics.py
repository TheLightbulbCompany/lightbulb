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


def test_internal_filter_excludes_by_person_not_distinct_id():
    with (
        patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "user_a, user_b"),
        patch.object(sm.settings, "PLATFORM_ADMIN_USER_IDS", "user_admin"),
    ):
        f = sm._internal_filter()
    assert "person_id not in (select person_id from person_distinct_ids where distinct_id in (" in f
    assert "is_internal" not in f
    for uid in ("user_a", "user_b", "user_admin"):
        assert f"'{uid}'" in f


def test_internal_filter_drops_unsafe_ids():
    with patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "ok_1,x') or 1=1 --"):
        f = sm._internal_filter()
    assert "'ok_1'" in f and "1=1" not in f.replace("1 = 1", "")


def test_queries_key_on_person_id_and_bound_task_scans():
    qs = sm.build_queries()
    for name in ("doers_weekly", "doers_7d", "activation", "retention", "funnel"):
        assert "person_id" in qs[name] and "uniq(distinct_id)" not in qs[name], name
    assert "l.distinct_id" not in qs["activation"] + qs["retention"]
    assert (
        "interval 24 hour" in qs["activation"]
        and "having min(timestamp) <= now() - interval 24 hour" in qs["activation"]
    )
    assert "having min(timestamp)" not in qs["retention"]
    for name in ("activation", "retention"):
        assert "interval 13 week" in qs[name], name


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
        "problem_kinds": [["crash", 1]],
        "funnel": [["app_first_launch", 20], ["task_completed", 12]],
        "unhealthy": [[2]],
        "seen_events": [["health", 5], ["problem_raised", 1]],
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
    # 2026-10-26 cohort: day 28 of its last install is 2026-11-30 > today -> not yet measurable
    assert next(c for c in d["cohorts"] if c["week"] == "2026-10-26")["w4_retention"] is None
    cur = next(c for c in d["cohorts"] if c["week"] == "2026-11-23")
    assert cur["w4_retention"] is None  # too young for a day-28 reading
    empty = next(c for c in d["cohorts"] if c["week"] == "2026-10-05")
    assert empty["activation_rate"] is None  # no installs: not a fake 0%
    assert d["task_success"]["weekly"][-1]["rate"] == pytest.approx(0.9)
    assert d["task_success"]["top_failed_reasons"][0] == {"key": "auth", "count": 5}
    assert [s["users"] for s in d["funnel"]] == [0, 20, 0, 0, 12, 0]
    assert d["reliability"]["unhealthy_installs_24h"] == 2
    assert d["task_success"]["top_problem_kinds"] == [{"key": "crash", "count": 1}]


def _rows(**over):
    base = {
        k: []
        for k in (
            "doers_weekly doers_7d activation retention tasks_weekly failed_reasons problem_kinds funnel unhealthy seen_events"
        ).split()
    }
    return {**base, **over}


def test_failed_query_fields_are_none_not_zero():
    failed = frozenset({"doers_weekly", "doers_7d", "activation", "tasks_weekly", "funnel"})
    d = sm.derive(_rows(seen_events=[["health", 1], ["problem_raised", 1]]), NOW, failed)
    assert d["wad"]["rolling_7d"] is None and d["wad"]["weekly"][0]["doers"] is None
    assert d["cohorts"][0]["installs"] is None and d["cohorts"][0]["activation_rate"] is None
    assert d["task_success"]["weekly"][0]["rate"] is None
    assert all(f["users"] is None for f in d["funnel"])
    assert d["failed_queries"] == sorted(failed)
    # queries that succeeded still give real answers
    assert d["reliability"]["unhealthy_installs_24h"] == 0


def test_never_seen_health_and_problem_events_are_none():
    d = sm.derive(_rows(), NOW)
    assert d["reliability"]["unhealthy_installs_24h"] is None
    assert d["task_success"]["top_problem_kinds"] is None
    d = sm.derive(_rows(), NOW, frozenset({"seen_events"}))  # can't tell -> unknown too
    assert d["reliability"]["unhealthy_installs_24h"] is None


def test_w4_maturity_boundary():
    # cohort Monday 2026-10-19: matures on 10-19 + 36 days = 2026-11-24
    d = sm.derive(_rows(), datetime(2026, 11, 23, 12, tzinfo=timezone.utc))
    assert next(c for c in d["cohorts"] if c["week"] == "2026-10-19")["w4_retention"] is None
    d = sm.derive(_rows(), datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    assert next(c for c in d["cohorts"] if c["week"] == "2026-10-19")["w4_retention"] is None  # no installs -> None
    d = sm.derive(_rows(activation=[["2026-10-19", "mac", 2, 1]]), datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    assert next(c for c in d["cohorts"] if c["week"] == "2026-10-19")["w4_retention"] == 0


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
    assert state == "ok" and data["wad"]["rolling_7d"] == 0
    assert data["reliability"]["unhealthy_installs_24h"] is None  # health never seen
    assert mock.await_count == calls  # second read served from cache


@pytest.mark.asyncio
async def test_compute_partial_on_one_failed_query_and_not_cached():
    async def fake(q, **_):
        if "event = 'problem_raised'" in q:
            return {"columns": [], "results": [], "stubbed": False, "error": "timeout"}
        return _ok([])

    with patch("core.services.posthog_admin.hogql", AsyncMock(side_effect=fake)):
        data, state, errors = await sm.compute()
    assert state == "partial" and data["failed_queries"] == ["problem_kinds"] and errors[0]["reason"] == "timeout"
    assert sm._cache is None
