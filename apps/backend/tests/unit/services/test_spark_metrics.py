"""Spark admin metrics: derive() over canned HogQL rows, compute() with posthog_admin.endpoint mocked."""

import asyncio
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
    first=None,
    perm_step=0,
    heard="",
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
        first.isoformat() if first else "1970-01-01T00:00:00+00:00",
        perm_step,
        heard,
    ]


def _rows(**over):
    names = (
        "doers_weekly doers_7d installs success_weekly success_version success_provider problems downloads"
        " voice_monthly subs setup_steps tasks_unfinished signals"
    )
    return {**{k: [] for k in names.split()}, **over}


def test_internal_filter_excludes_by_person_and_drops_unsafe_ids():
    with (
        patch.object(sm.settings, "SPARK_INTERNAL_USER_IDS", "user_a, x') or 1=1 --"),
        patch.object(sm.settings, "PLATFORM_ADMIN_USER_IDS", "user_admin"),
    ):
        f = sm._internal_filter()
    assert "person_id not in (select person_id from person_distinct_ids" in f
    assert "'user_a'" in f and "'user_admin'" in f and "1=1" not in f


def test_installs_are_released_first_launches_and_usage_needs_an_account():
    f = sm._account_filter()
    qs = sm.build_queries()
    first = (
        qs["installs"].split("from events where event = 'app_first_launch'")[1].split("group by person_id having")[0]
    )
    assert sm._release_install() in first and f not in first  # every released first launch, signed in or not
    assert "if(" + f + ", 1, 0) as has_account" in qs["installs"]  # "signed in" = the person owns an account
    assert "countIf(e.event = 'signin_completed'" not in qs["installs"]
    for name, q in qs.items():
        if name not in ("installs", "downloads"):
            assert f in q, name  # usage: signed-in people only
    assert f not in qs["downloads"] and sm._release_install() not in qs["downloads"]


def test_queries_define_human_task_bound_scans_and_count_at_most_thirteen():
    qs = sm.build_queries()
    assert len(qs) <= 13  # 12 + `signals`
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
    with patch("core.services.posthog_admin.endpoint", AsyncMock(return_value=stub)):
        data, state, errors = await sm.compute()
    assert data is None and state == "unavailable" and errors[0]["reason"] == "not configured"


@pytest.mark.asyncio
async def test_compute_ok_is_cached():
    mock = AsyncMock(return_value=_ok([]))
    with patch("core.services.posthog_admin.endpoint", mock):
        data, state, _ = await sm.compute()
        calls = mock.await_count
        await sm.compute()
    assert state == "ok" and data["wad"]["last7"] == 0 and mock.await_count == calls


@pytest.mark.asyncio
async def test_failed_query_degrades_only_its_sections_and_is_cached_briefly():
    async def fake(stem, q, **_):
        return (
            {"columns": [], "results": [], "stubbed": False, "error": "timeout"}
            if "spark_download_clicked" in q
            else _ok([])
        )

    mock = AsyncMock(side_effect=fake)
    with patch("core.services.posthog_admin.endpoint", mock):
        data, state, errors = await sm.compute()
        calls = mock.await_count
        await sm.compute()
    assert state == "partial" and errors == [{"domain": "posthog:downloads", "reason": "timeout"}]
    assert data["unavailable"] == ["downloads"] and data["wad"]["last7"] == 0  # other sections still there
    assert mock.await_count == calls  # failure served from the 60s cache
    assert sm._cache[0] - __import__("time").monotonic() <= sm.FAIL_TTL_S


@pytest.mark.asyncio
async def test_compute_runs_three_queries_at_a_time():
    """PostHog runs 3 API queries at once per project and queues the rest: sent all together, the queued ones spent
    their whole client timeout waiting in line and their sections showed "couldn't load" (seen in prod 2026-11-26)."""
    running = peak = 0

    async def fake(stem, q, **_):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return _ok([])

    with patch("core.services.posthog_admin.endpoint", AsyncMock(side_effect=fake)) as mock:
        _, state, _ = await sm.compute()
    assert state == "ok" and peak == sm.PARALLEL == 3 and mock.await_count == len(sm.QUERY_KEYS)


@pytest.mark.asyncio
async def test_a_query_that_cannot_start_inside_the_budget_is_a_timeout_and_is_not_sent(monkeypatch):
    monkeypatch.setattr(sm, "BUDGET_S", 0.0)
    with patch("core.services.posthog_admin.endpoint", AsyncMock(return_value=_ok([]))) as mock:
        data, state, errors = await sm.compute()
    assert mock.await_count == 0 and state == "partial"
    assert {e["reason"] for e in errors} == {"timeout"} and len(errors) == len(sm.QUERY_KEYS)
    assert set(data["unavailable"]) == {k for keys in sm.QUERY_KEYS.values() for k in keys}


@pytest.mark.asyncio
async def test_each_query_gets_the_time_left_in_the_budget_never_more_than_its_own_timeout(monkeypatch):
    monkeypatch.setattr(sm, "BUDGET_S", 4.0)
    with patch("core.services.posthog_admin.endpoint", AsyncMock(return_value=_ok([]))) as mock:
        await sm.compute()
    assert all(0 < c.kwargs["timeout_s"] <= 4.0 for c in mock.await_args_list)
    monkeypatch.setattr(sm, "BUDGET_S", 600.0)
    sm._cache = None
    with patch("core.services.posthog_admin.endpoint", AsyncMock(return_value=_ok([]))) as mock:
        await sm.compute()
    assert {c.kwargs["timeout_s"] for c in mock.await_args_list} == {sm.QUERY_TIMEOUT_S}


@pytest.mark.asyncio
async def test_each_query_is_a_saved_endpoint_named_for_this_environment(monkeypatch):
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "prod")
    with patch("core.services.posthog_admin.endpoint", AsyncMock(return_value=_ok([]))) as mock:
        await sm.compute()
    queries = sm.build_queries()
    assert {c.args for c in mock.await_args_list} == {(f"spark_prod_{n}", q) for n, q in queries.items()}


@pytest.mark.asyncio
async def test_generated_at_is_the_oldest_time_posthog_computed_a_query():
    times = iter(["2026-11-26T16:15:03+00:00", "2026-11-26T16:00:20Z"])

    async def fake(stem, q, **_):
        return {**_ok([]), "as_of": next(times, "2026-11-26T16:30:00+00:00")}

    with patch("core.services.posthog_admin.endpoint", AsyncMock(side_effect=fake)):
        data, _, _ = await sm.compute()
    assert data["generated_at"] == "2026-11-26T16:00:20+00:00"


def test_every_query_has_section_keys():
    assert set(sm.QUERY_KEYS) == set(sm.build_queries())


def test_truncated_flag_at_row_cap_and_installs_query_shape():
    rows = [install(NOW - timedelta(days=1))] * sm.INSTALL_CAP
    assert sm.derive(_rows(installs=rows), NOW)["truncated"] is True
    q = sm.build_queries()["installs"]
    assert "having launch >= now() - interval 84 day" in q and "from persons" in q and "person.properties" not in q


# ---- money, setup, sources, funnel fix -------------------------------------------------------------


def test_new_query_definitions():
    qs = sm.build_queries()
    assert (
        "event = 'voice_session_ended'" in qs["voice_monthly"] and "toInt(v.properties.seconds)" in qs["voice_monthly"]
    )
    assert "interval 6 month" in qs["voice_monthly"] and "toStartOfMonth(toTimeZone(" in qs["voice_monthly"]
    for e in ("account_created", "subscription_started", "subscription_updated", "subscription_cancelled"):
        assert f"'{e}'" in qs["subs"]
    assert "order by timestamp" in qs["subs"] and "interval 12 month" in qs["subs"]
    assert "interval 30 day" in qs["setup_steps"] and "uniq(person_id)" in qs["setup_steps"]
    assert "minIf(e.timestamp," in qs["installs"] and "!= 'automation'" in qs["installs"]
    assert "'onboarding_step'" in qs["installs"] and "spark_heard_from" in qs["installs"]
    q = qs["tasks_unfinished"]
    assert "task_id" in q and "interval 30 minute" in q and "interval 30 day" in q and "fin = 0" in q
    assert "event in ('task_completed', 'task_failed', 'task_stopped')" in q  # a stop is a finish
    assert q.count("'task_stopped'") == 2  # the finish count and the outer event list
    for q in qs.values():  # every query is team-filtered
        assert "person_id not in" in q or "1 = 1" in q


def sub(person, event, month="2026-11-01", to="", reason="", feedback=""):
    return [person, event, month, to, reason, feedback]


def test_money_mrr_paying_and_unknown_rule():
    subs = [
        sub("a", "subscription_started"),
        sub("a", "subscription_updated", to="trialing"),
        sub("a", "subscription_updated", to="active"),
        sub("b", "subscription_updated", to="active"),
        sub("c", "subscription_updated", to="trialing"),
        sub("c", "subscription_updated", to="canceled"),
        sub("d", "subscription_started"),  # old data: no update -> unknown, not paying
    ]
    with patch.object(sm.settings, "SPARK_PRICE_USD", 20):
        m = sm.derive(_rows(subs=subs), NOW)["money"]["subs"]
    assert m["paying"] == 2 and m["mrr_usd"] == 40 and m["unknown"] == 1


def test_money_no_data_when_events_never_seen():
    m = sm.derive(_rows(subs=[sub("d", "subscription_started")]), NOW)["money"]
    assert m["subs"] is None and m["voice"] is None  # paying cannot be told without any update


def test_trial_to_paid_and_cancellations_by_month():
    subs = [
        sub("a", "account_created", "2026-10-01"),
        sub("a", "subscription_updated", "2026-10-01", "active"),
        sub("b", "account_created", "2026-10-01"),
        sub("c", "account_created", "2026-09-01"),
        sub("a", "subscription_cancelled", "2026-11-01", reason="cancellation_requested", feedback="too_expensive"),
        sub("e", "subscription_cancelled", "2026-11-01", reason="payment_failed"),
    ]
    m = sm.derive(_rows(subs=subs), NOW)["money"]["subs"]
    assert m["trial_to_paid"] == [
        {"month": "2026-10", "k": 1, "n": 2, "partial": True}
    ]  # first update month  # Sept predates the first update
    c = m["cancellations"][0]
    assert c["month"] == "2026-11" and c["count"] == 2
    assert {"key": "payment_failed", "count": 1} in c["reasons"] and {"key": "too_expensive", "count": 1} in c[
        "feedback"
    ]
    assert {"key": "none", "count": 1} in c["feedback"]


def test_trial_to_paid_partial_flags_first_update_and_current_month():
    subs = [
        sub("a", "account_created", "2026-09-01"),
        sub("a", "subscription_updated", "2026-09-01", "active"),
        sub("b", "account_created", "2026-10-01"),
        sub("c", "account_created", "2026-11-01"),
    ]
    rows = sm.derive(_rows(subs=subs), NOW)["money"]["subs"]["trial_to_paid"]
    assert [(r["month"], r["partial"]) for r in rows] == [("2026-09", True), ("2026-10", False), ("2026-11", True)]


def test_server_event_queries_read_spark_product_only():
    qs = sm.build_queries()
    for name in ("subs", "voice_monthly"):
        assert (
            "toString(properties.product) = 'spark'" in qs[name]
            and "coalesce(nullIf(toString(properties.product)" not in qs[name]
        )


def test_voice_months_percentiles_cost_and_top_margin():
    voice = [
        ["2026-11-01", "a", 6000, 5, "a@x.co"],  # 100 min, paying
        ["2026-11-01", "b", 600, 1, ""],  # 10 min, no status
        ["2026-10-01", "a", 60, 1, "a@x.co"],
    ]
    subs = [sub("a", "subscription_updated", to="active")]
    with patch.object(sm.settings, "SPARK_PRICE_USD", 20):
        money = sm.derive(_rows(voice_monthly=voice, subs=subs), NOW)["money"]
    nov = money["voice"]["months"][1]
    assert nov == {"month": "2026-11", "minutes": 110.0, "users": 2, "p50": 55.0, "p90": 100.0, "cost_usd": 5.5}
    assert money["voice"]["top"][0] == {
        "email": "a@x.co",
        "status": "active",
        "minutes": 100.0,
        "cost_usd": 5.0,
        "margin_usd": 15.0,
    }
    assert money["voice"]["top"][1]["status"] == "none" and money["voice"]["top"][1]["margin_usd"] == -0.5


def test_time_to_first_task_buckets_only_settled_installs_in_30d():
    def at(minutes_after, **kw):
        launch = NOW - timedelta(days=5)
        return install(launch, tasks=1, first=launch + timedelta(minutes=minutes_after), **kw)

    rows = [
        at(4),
        at(29),
        at(119),
        at(1439),
        at(1441),
        install(NOW - timedelta(days=5)),
        install(NOW - timedelta(hours=2)),
        install(NOW - timedelta(days=40)),
    ]
    t = sm.derive(_rows(installs=rows), NOW)["time_to_first_task"]
    assert {b["bucket"]: b["k"] for b in t} == {"lt5m": 1, "lt30m": 1, "lt2h": 1, "lt24h": 1, "later": 1, "never": 1}
    assert all(b["n"] == 6 for b in t)


def test_heard_from_table_and_no_data_when_nobody_answered():
    old = NOW - timedelta(days=3)
    rows = [
        install(old, act24=1, heard="youtube"),
        install(old, heard="youtube"),
        install(old),
        install(NOW - timedelta(hours=1), heard="reddit"),
    ]
    h = sm.derive(_rows(installs=rows), NOW)["heard_from"]
    assert h[0] == {"choice": "youtube", "installs": 2, "activated_24h": {"k": 1, "n": 2}}
    assert {"choice": "reddit", "installs": 1, "activated_24h": {"k": 0, "n": 0}} in h
    assert sm.derive(_rows(installs=[install(old)]), NOW)["heard_from"] is None


def test_windows_install_reaches_permission_step_via_brain_or_step_event():
    old = NOW - timedelta(days=3)
    rows = [
        install(old, platform="windows", signin=1, brain=1),
        install(old, platform="windows", signin=1),
        install(old, platform="mac", brain=1),
        install(old, platform="mac", perm_step=1),
    ]
    f = sm.derive(_rows(installs=rows), NOW)["funnel"]
    assert [x["k"] for x in f][:4] == [
        4,
        2,
        2,
        2,
    ]  # launch, signin, brain (win+mac), permission (win via brain, mac via step)
    assert f[3]["step"] == "permission"


def test_setup_section():
    steps = [
        ["onboarding_step", "brain", "shown", 10],
        ["onboarding_step", "brain", "completed", 6],
        ["onboarding_step", "brain", "skipped", 1],
        ["brain_signin_failed", "timeout", "", 3],
        ["permission_prompted", "microphone", "", 8],
        ["permission_denied", "microphone", "", 2],
    ]
    s = sm.derive(_rows(setup_steps=steps, tasks_unfinished=[[3, 12]]), NOW)["setup"]
    assert s["steps"][1] == {"step": "brain", "shown": 10, "completed": {"k": 6, "n": 10}, "skipped": {"k": 1, "n": 10}}
    assert s["steps"][0]["shown"] == 0
    assert s["signin_failures"] == [{"key": "timeout", "count": 3}]
    assert s["permissions"] == [{"permission": "microphone", "prompted": 8, "denied": {"k": 2, "n": 8}}]
    assert s["unfinished"] == {"k": 3, "n": 12}


def test_setup_no_data_when_onboarding_never_seen():
    assert sm.derive(_rows(), NOW)["setup"] is None
    assert sm.derive(_rows(setup_steps=[["onboarding_step", "brain", "shown", 1]]), NOW)["setup"]["unfinished"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["voice_monthly", "subs", "setup_steps", "tasks_unfinished", "signals"])
async def test_a_failed_new_query_marks_its_section_unavailable(failing):
    names = list(sm.build_queries())

    async def fake(stem, q, **_):
        bad = sm.build_queries()[failing]
        return {"columns": [], "results": [], "stubbed": False, "error": "boom" if q == bad else None}

    with patch("core.services.posthog_admin.endpoint", AsyncMock(side_effect=fake)):
        data, state, errors = await sm.compute()
    assert state == "partial" and errors[0]["domain"] == f"posthog:{failing}" and failing in names
    assert data["unavailable"] == list(sm.QUERY_KEYS[failing])


# ---- signals: stops, automations, features, crashes, versions ---------------------------------------


def test_signals_query_reads_the_new_events_and_never_feeds_the_doer_or_success_queries():
    qs = sm.build_queries()
    q = qs["signals"]
    for e in (
        "task_stopped",
        "automation_fired",
        "feature_used",
        "voice_session_ended",
        "app_opened",
        "update_installed",
    ):
        assert f"'{e}'" in q, e
    assert "interval 30 day" in q
    assert "!= 'automation'" in q  # started n counts human tasks only
    assert "properties.how" in q and "properties.feature" in q
    for name in ("doers_weekly", "doers_7d", "success_weekly", "success_version", "success_provider"):
        for e in ("task_stopped", "automation_fired", "feature_used"):
            assert e not in qs[name], (name, e)


# columns: event, key, count, people 30d, people 7d, people with full access
SIGNALS = [
    ["task_started", "-", 12, 4, 3, 0],
    ["task_stopped", "pressed", 2, 2, 1, 0],
    ["task_stopped", "said", 1, 1, 1, 0],
    ["automation_fired", "true/true", 5, 2, 2, 0],
    ["automation_fired", "true/false", 3, 3, 1, 0],
    ["automation_fired", "false/true", 2, 1, 1, 0],
    ["automation_fired", "*", 10, 5, 3, 0],
    ["feature_used", "dictation", 6, 3, 2, 0],
    ["feature_used", "marks", 1, 1, 0, 0],
    ["feature_used", "automation_created", 2, 2, 1, 0],
    ["voice_session_ended", "hold", 20, 4, 3, 2],
    ["voice_session_ended", "typed", 4, 1, 1, 1],
    ["voice_session_ended", "-", 1, 1, 1, 0],
    ["voice_session_ended", "*", 25, 7, 5, 2],
    ["problem_raised", "-", 3, 2, 2, 0],
    ["app_opened", "1.0", 50, 6, 4, 0],
    ["app_opened", "1.1", 30, 3, 3, 0],
    ["app_opened", "-", 1, 1, 1, 0],
    ["update_installed", "1.9", 1, 1, 1, 0],
    ["update_installed", "1.10", 2, 2, 2, 0],
]


def test_signals_stops_as_k_of_started_with_how_split():
    s = sm.derive(_rows(signals=SIGNALS), NOW)["signals"]
    assert s["stopped"] == {
        "k": 3,
        "n": 12,
        "how": [{"key": "pressed", "count": 2}, {"key": "said", "count": 1}, {"key": "other", "count": 0}],
    }
    odd = [
        ["task_started", "-", 1, 1, 1, 0],
        ["task_stopped", "pressed", 3, 1, 1, 0],
        ["task_stopped", "-", 1, 1, 1, 0],
    ]
    s = sm.derive(_rows(signals=odd), NOW)["signals"]["stopped"]  # a missing how folds into other; k can exceed n
    assert s == {
        "k": 4,
        "n": 1,
        "how": [{"key": "pressed", "count": 3}, {"key": "said", "count": 0}, {"key": "other", "count": 1}],
    }


def test_signals_automations_started_k_of_runs_and_brain():
    a = sm.derive(_rows(signals=SIGNALS), NOW)["signals"]["automations"]
    assert a["runs"] == 10 and a["ok"] == {"k": 8, "n": 10} and a["with_brain"] == {"k": 7, "n": 10}
    assert a["people_30d"] == 5  # from the `*` row: exact, not the biggest per-key row


def test_signals_features_ordered_by_people_with_full_access_from_the_sixth_column():
    f = sm.derive(_rows(signals=SIGNALS), NOW)["signals"]["features"]
    assert f[0]["feature"] == "automation ran"  # 5 people, the most
    assert {"feature": "push to talk", "people_30d": 4, "people_7d": 3} in f
    by = {x["feature"]: x for x in f}
    assert by["full access"] == {"feature": "full access", "people_30d": 2, "people_7d": None}  # only a 30d column
    assert by["typing"] == {"feature": "typing", "people_30d": 1, "people_7d": 1}
    assert by["dictation"]["people_30d"] == 3 and by["marks"]["people_30d"] == 1
    assert by["automation ran"] == {"feature": "automation ran", "people_30d": 5, "people_7d": 3}
    assert by["created an automation"] == {"feature": "created an automation", "people_30d": 2, "people_7d": 1}
    assert by["unknown"]["people_30d"] == 1  # a missing voice kind
    keys = [(-x["people_30d"], x["feature"]) for x in f]
    assert keys == sorted(keys)  # ties break by label, so a refresh never reorders them


def test_signals_crash_versions_and_updates():
    s = sm.derive(_rows(signals=SIGNALS), NOW)["signals"]
    # crash people 7d / sum of per-version people 7d (about)
    assert s["crash"] == {"k": 2, "n": 8}
    assert s["versions"] == [
        {"version": "1.0", "people_7d": 4},
        {"version": "1.1", "people_7d": 3},
        {"version": "unknown", "people_7d": 1},
    ]
    # newest first, as numbers: 1.10 is newer than 1.9
    assert s["updates"] == [{"to_version": "1.10", "installs_30d": 2}, {"to_version": "1.9", "installs_30d": 1}]


def test_signals_parts_are_none_until_their_events_exist():
    assert sm.derive(_rows(), NOW)["signals"] == {
        "stopped": None,
        "automations": None,
        "features": None,
        "crash": None,
        "versions": None,
        "updates": None,
    }
    only_open = sm.derive(_rows(signals=[["app_opened", "1.0", 5, 2, 2, 0]]), NOW)["signals"]
    assert only_open["stopped"] is None and only_open["automations"] is None and only_open["features"] is None
    assert only_open["crash"] == {"k": 0, "n": 2} and only_open["versions"] == [{"version": "1.0", "people_7d": 2}]


def test_signals_people_come_from_the_star_rows_not_the_biggest_key():
    rows = [
        ["voice_session_ended", "hold", 9, 2, 2, 2],  # two full-access people who only hold
        ["voice_session_ended", "handsFree", 9, 3, 3, 3],  # three others who only use hands-free
        ["voice_session_ended", "*", 18, 5, 5, 5],
        ["automation_fired", "true/true", 4, 2, 1, 0],  # different people in each key
        ["automation_fired", "true/false", 3, 3, 2, 0],
        ["automation_fired", "*", 7, 5, 3, 0],
    ]
    s = sm.derive(_rows(signals=rows), NOW)["signals"]
    by = {f["feature"]: f for f in s["features"]}
    assert by["full access"]["people_30d"] == 5
    assert by["automation ran"] == {"feature": "automation ran", "people_30d": 5, "people_7d": 3}
    assert s["automations"]["people_30d"] == 5 and s["automations"]["runs"] == 7
    assert not any("*" in f["feature"] for f in s["features"])
    assert s["versions"] is None and s["updates"] is None


def test_signals_automations_are_no_data_without_their_star_row():
    rows = [["automation_fired", "true/true", 4, 2, 1, 0], ["voice_session_ended", "hold", 3, 2, 2, 1]]
    s = sm.derive(_rows(signals=rows), NOW)["signals"]
    assert s["automations"] is None
    assert [f["feature"] for f in s["features"]] == ["push to talk"]  # no full access, no automation ran


def test_signals_query_unions_exact_totals_with_the_team_filter_and_30_days_in_both_halves():
    q = sm.build_queries()["signals"]
    assert q.count("union all") == 1 and "'*' as k" in q
    first, second = q.split("union all")
    for half in (first, second):
        assert "timestamp >= now() - interval 30 day" in half and "person_id not in" in half or "1 = 1" in half
        assert "toString(properties.product) = 'spark'" in half
