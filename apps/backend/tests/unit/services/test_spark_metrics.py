"""Spark admin metrics: derive() over canned HogQL rows, compute() with posthog_admin.hogql mocked."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import spark_metrics as sm  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)  # Tuesday; current week = 2026-11-23 (partial)


def _ok(results):
    return {"columns": [], "results": results, "stubbed": False, "error": None}


@pytest.fixture(autouse=True)
def _no_cache():
    sm._cache = None
    yield
    sm._cache = None


def install(
    launch,
    *,
    platform="mac",
    channel="unknown",
    signin=0,
    brain=0,
    perm=0,
    onb=0,
    tasks=0,
    act24=0,
    d7=0,
    w4=0,
    sub=0,
    last="",
):
    """One `installs` row (column order documented in build_queries)."""
    return [
        launch.isoformat(),
        platform,
        "1.0",
        "chatgpt",
        channel,
        "",
        signin,
        brain,
        perm,
        onb,
        tasks,
        act24,
        d7,
        w4,
        sub,
        last,
    ]


def _rows(**over):
    names = "doers_weekly doers_7d installs success_weekly success_version success_provider problems downloads"
    return {**{k: [] for k in names.split()}, **over}


def test_internal_filter_excludes_by_person_and_drops_unsafe_ids():
    with (
        patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "user_a, x') or 1=1 --"),
        patch.object(sm.settings, "PLATFORM_ADMIN_USER_IDS", "user_admin"),
    ):
        f = sm._internal_filter()
    assert "person_id not in (select person_id from person_distinct_ids" in f
    assert "'user_a'" in f and "'user_admin'" in f and "1=1" not in f


def test_queries_define_human_task_bound_scans_and_count_at_most_eight():
    qs = sm.build_queries()
    assert len(qs) <= 8
    for name in ("doers_weekly", "doers_7d", "success_weekly", "success_version"):
        assert "!= 'automation'" in qs[name], name
    assert ">= 2" in qs["doers_weekly"] and "uniq(toDate(toTimeZone(timestamp, 'UTC')))" in qs["doers_weekly"]
    assert "toStartOfWeek(toTimeZone(timestamp, 'UTC'), 1)" in qs["doers_weekly"]
    assert "e.timestamp <= l.launch + interval 24 hour" in qs["installs"]  # any task in [launch, +24h]
    for name, q in qs.items():
        assert "timestamp >= now() - interval" in q, name
        assert "health" not in q
    assert "seen_events" not in qs


def test_automation_makes_no_doer_but_derive_counts_what_sql_returns():
    # The SQL excludes automation (asserted above); derive reports the counts it is given, weekly + rolling.
    d = sm.derive(_rows(doers_weekly=[["2026-11-16", 4]], doers_7d=[["last7", 3], ["prev7", 2]]), NOW)
    assert d["wad"]["last7"] == 3 and d["wad"]["prev7"] == 2
    assert [w["doers"] for w in d["wad"]["weekly"]][-2:] == [4, 0]


def test_current_week_is_partial_on_a_tuesday():
    d = sm.derive(_rows(), NOW)
    weeks = d["wad"]["weekly"]
    assert weeks[-1] == {"week": "2026-11-23", "partial": True, "doers": 0}
    assert not any(w["partial"] for w in weeks[:-1])


def test_returning_user_with_old_task_and_task_after_launch_is_activated():
    # act24 counts ANY human task in [launch, launch+24h], not the person's first-ever task.
    launch = NOW - timedelta(days=3)
    d = sm.derive(_rows(installs=[install(launch, tasks=2, act24=1)]), NOW)
    assert d["cohorts"][0]["activated_24h"] == {"k": 1, "n": 1}
    assert d["channels"] == [{"channel": "unknown", "activated_24h": {"k": 1, "n": 1}}]


def test_unsettled_install_is_not_in_the_activation_denominator():
    d = sm.derive(_rows(installs=[install(NOW - timedelta(hours=2))]), NOW)
    c = d["cohorts"][0]
    assert c["installs"] == 1 and c["activated_24h"] == {"k": 0, "n": 0}


def test_w4_denominator_is_activated_installs_only():
    launch = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)  # cohort 2026-10-12: mature (+36d = 11-17)
    rows = [install(launch, tasks=3, act24=1, w4=2), install(launch, tasks=1, act24=0, w4=1)]
    d = sm.derive(_rows(installs=rows), NOW)
    assert d["cohorts"][0]["w4"] == {"k": 1, "n": 1}


def test_w4_maturity_boundary():
    launch = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)  # week start + 36d = 2026-11-24 00:00
    rows = _rows(installs=[install(launch, act24=1)])
    assert sm.derive(rows, datetime(2026, 11, 23, 23, 59, tzinfo=timezone.utc))["cohorts"][0]["w4"] is None
    assert sm.derive(rows, datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))["cohorts"][0]["w4"] == {"k": 0, "n": 1}


def test_d7_only_counts_installs_a_week_old():
    old, young = NOW - timedelta(days=10), NOW - timedelta(days=2)
    d = sm.derive(_rows(installs=[install(old, d7=2, act24=1), install(young, d7=2, act24=1)]), NOW)
    assert sum(c["activated_d7"]["n"] for c in d["cohorts"]) == 1
    assert sum(c["activated_d7"]["k"] for c in d["cohorts"]) == 1


def test_funnel_people_and_furthest_step():
    launch = NOW - timedelta(days=9)
    row = install(
        launch, signin=1, brain=1, perm=1, onb=1, tasks=4, act24=1, d7=3, channel="referral", last="2026-11-20"
    )
    d = sm.derive(_rows(installs=[row, install(NOW - timedelta(days=1))]), NOW)
    assert [f["k"] for f in d["funnel"]] == [2, 1, 1, 1, 1, 1, 1, 0]
    assert [f["n"] for f in d["funnel"]] == [2, 2, 2, 2, 2, 2, 1, 2]  # d7 step: only installs 7d+ old
    assert d["funnel"][6]["step"] == "activated d7 (installs 7d+ old)" and d["truncated"] is False
    p = d["people"][0]
    assert p["furthest"] == "activated d7" and p["last_task"] == "2026-11-20" and p["channel"] == "referral"
    assert d["people"][1]["furthest"] == "first launch" and d["people"][1]["last_task"] is None


def test_success_problems_and_brain_unavailable():
    d = sm.derive(
        _rows(
            success_weekly=[["2026-11-23", "mac", 9, 1]],
            success_provider=[["claude", 4, 4]],
            problems=[
                ["task_failed", "auth", 5, 0],
                ["problem_raised", "brain.unavailable", 3, 2],
                ["problem_raised", "crash", 1, 1],
            ],
            downloads=[["hero", 7]],
        ),
        NOW,
    )
    assert d["success"]["weekly"][0]["success"] == {"k": 9, "n": 10}
    assert d["success"]["by_provider"] == [{"key": "claude", "success": {"k": 4, "n": 8}}]
    assert d["success"]["failed_reasons"] == [{"key": "auth", "count": 5}]
    assert [k["key"] for k in d["success"]["problem_kinds"]] == ["brain.unavailable", "crash"]
    assert d["brain_unavailable_installs_24h"] == 2 and d["downloads"] == [{"key": "hero", "count": 7}]


@pytest.mark.asyncio
async def test_compute_stubbed_when_posthog_unconfigured():
    stub = {"columns": [], "results": [], "stubbed": True, "error": None}
    with patch("core.services.posthog_admin.hogql", AsyncMock(return_value=stub)):
        data, state, errors = await sm.compute()
    assert data is None and state == "unavailable" and errors[0]["reason"] == "not configured"


@pytest.mark.asyncio
async def test_compute_ok_is_cached():
    mock = AsyncMock(return_value=_ok([]))
    with patch("core.services.posthog_admin.hogql", mock):
        data, state, _ = await sm.compute()
        calls = mock.await_count
        await sm.compute()
    assert state == "ok" and data["wad"]["last7"] == 0 and mock.await_count == calls


@pytest.mark.asyncio
async def test_any_failed_query_means_no_figures_and_is_cached_briefly():
    async def fake(q, **_):
        return (
            {"columns": [], "results": [], "stubbed": False, "error": "timeout"}
            if "spark_download_clicked" in q
            else _ok([])
        )

    mock = AsyncMock(side_effect=fake)
    with patch("core.services.posthog_admin.hogql", mock):
        data, state, errors = await sm.compute()
        calls = mock.await_count
        await sm.compute()
    assert data is None and state == "partial" and errors == [{"domain": "posthog:downloads", "reason": "timeout"}]
    assert mock.await_count == calls  # failure served from the 60s cache
    assert sm._cache[0] - __import__("time").monotonic() <= sm.FAIL_TTL_S


def test_truncated_flag_at_row_cap_and_installs_query_shape():
    rows = [install(NOW - timedelta(days=1))] * sm.INSTALL_CAP
    assert sm.derive(_rows(installs=rows), NOW)["truncated"] is True
    q = sm.build_queries()["installs"]
    assert "having launch >= now() - interval 84 day" in q and "from persons" in q and "person.properties" not in q
