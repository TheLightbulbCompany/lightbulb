"""derive(): the DECIDED metric set over fixture rows.

Fixture: 3 external owners + 1 internal.
  ext_a  paid, container ready, 1 armed routine, autonomous successes in each of the
         last 4 completed weeks -> activated + counts toward the headline bar
  ext_b  paid, container ready, nightly-CEO only (must NOT count), one manual run
  ext_c  expired trial, never ready
  int_x  internal, paid, armed, running every week -> must be absent from external
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import growth  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # a Wednesday; week = 2026-08-24


def _week(n_back: int) -> str:
    monday = (NOW - timedelta(days=NOW.weekday())).date() - timedelta(weeks=n_back)
    return monday.isoformat()


def _fixture():
    billing = [
        {
            "owner_id": "ext_a",
            "subscription_status": "active",
            "plan": "standard",
            "provider_choice": "chatgpt_oauth",
            "created_at": "2026-07-01T00:00:00Z",
            "nightly_ceo_armed_company": "c_a",
        },
        {
            "owner_id": "ext_b",
            "subscription_status": "active",
            "plan": "ultra_pro",
            "provider_choice": "byo_key",
            "created_at": "2026-08-20T00:00:00Z",
            "nightly_ceo_opted_out": True,
        },
        {
            "owner_id": "ext_c",
            "subscription_status": "trial_local",
            "trial_end": 1_700_000_000,
            "created_at": "2026-08-20T00:00:00Z",
        },
        {
            "owner_id": "int_x",
            "subscription_status": "active",
            "plan": "standard",
            "provider_choice": "chatgpt_oauth",
            "created_at": "2026-07-01T00:00:00Z",
        },
    ]
    containers = [
        {"owner_id": "ext_a", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
        {"owner_id": "ext_b", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
        {"owner_id": "ext_c", "status": "stopped"},
        {"owner_id": "int_x", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
    ]
    paperclip = [
        {
            "user_id": "ext_a",
            "company_id": "c_a",
            "seeded_agent_ids": ["f"],
            "status": "active",
            "bootstrap_state": "delivered",
        },
        {
            "user_id": "ext_b",
            "company_id": "c_b",
            "seeded_agent_ids": ["f"],
            "status": "active",
            "bootstrap_state": "failed",
        },
        {
            "user_id": "ext_c",
            "company_id": "c_c",
            "seeded_agent_ids": ["f"],
            "status": "active",
            "bootstrap_state": "not_started",
        },
        {
            "user_id": "u_int",
            "org_id": "int_x",
            "company_id": "c_x",
            "seeded_agent_ids": ["f"],
            "status": "active",
            "bootstrap_state": "delivered",
        },
        {"user_id": "__pending_brief__:zzz"},
    ]
    integrations = [
        {"owner_id": "ext_a", "provider": "github", "status": "connected"},
        {"owner_id": "ext_a", "provider": "stripe", "status": "connected"},
        {"owner_id": "ext_b", "provider": "google", "status": "connected"},
    ]
    companies = [
        {"id": "c_a", "name": "Alpha"},
        {"id": "c_b", "name": "Beta"},
        {"id": "c_c", "name": "Gamma"},
        {"id": "c_x", "name": "Lightbulb AI"},
    ]
    old = "2026-06-01T00:00:00Z"
    routines = [
        {
            "company_id": "c_a",
            "id": "r1",
            "title": "Daily report",
            "status": "active",
            "auto_paused": False,
            "created_at": old,
            "updated_at": old,
            "has_enabled_trigger": True,
            "trigger_kinds": "schedule",
        },
        {
            "company_id": "c_a",
            "id": "r2",
            "title": "Old one",
            "status": "archived",
            "auto_paused": False,
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-22T00:00:00Z",
            "has_enabled_trigger": False,
            "trigger_kinds": None,
        },
        {
            "company_id": "c_b",
            "id": "r3",
            "title": growth.NIGHTLY_CEO_ROUTINE_TITLE,
            "status": "active",
            "auto_paused": False,
            "created_at": old,
            "updated_at": old,
            "has_enabled_trigger": True,
            "trigger_kinds": "schedule",
        },
        {
            "company_id": "c_b",
            "id": "r4",
            "title": "Broken",
            "status": "paused",
            "auto_paused": True,
            "created_at": old,
            "updated_at": "2026-08-10T00:00:00Z",
            "has_enabled_trigger": True,
            "trigger_kinds": "webhook",
        },
        {
            "company_id": "c_x",
            "id": "r5",
            "title": "Internal",
            "status": "active",
            "auto_paused": False,
            "created_at": old,
            "updated_at": old,
            "has_enabled_trigger": True,
            "trigger_kinds": "schedule",
        },
    ]
    runs = []
    for back in range(0, 6):  # current week + 5 previous
        runs.append({"company_id": "c_a", "week": _week(back), "kind": "autonomous", "ok": 2, "total": 3})
        runs.append({"company_id": "c_x", "week": _week(back), "kind": "autonomous", "ok": 9, "total": 9})
    runs.append({"company_id": "c_b", "week": _week(1), "kind": "nightly_ceo", "ok": 5, "total": 5})
    runs.append({"company_id": "c_b", "week": _week(1), "kind": "human", "ok": 1, "total": 1})
    first_autonomous = [
        {"company_id": "c_a", "first_ok": "2026-07-10T00:00:00Z"},
        {"company_id": "c_x", "first_ok": "2026-07-01T00:00:00Z"},
    ]
    runs_daily = [
        {"company_id": "c_a", "day": "2026-08-25", "kind": "autonomous", "ok": 2, "total": 3},
        {"company_id": "c_a", "day": "2026-08-24", "kind": "autonomous", "ok": 1, "total": 1},
    ]
    gate_blocked = {
        "totals": [{"code": "trial_expired", "n": 7}],
        "daily": [{"day": "2026-08-25", "code": "trial_expired", "n": 7}],
    }
    posthog_runs = [[_week(1), "autonomous", 2], [_week(1), "human", 10]]
    posthog_funnel = [[_week(1), "trial_started", 4]]
    posthog_funnel_daily = [["2026-08-25", "trial_started", 1]]
    posthog_sources = [["reddit.com", 3], ["direct", 2]]
    return dict(
        billing=billing,
        containers=containers,
        paperclip=paperclip,
        integrations=integrations,
        companies=companies,
        routines=routines,
        runs=runs,
        first_autonomous=first_autonomous,
        gate_blocked=gate_blocked,
        posthog_runs=posthog_runs,
        posthog_funnel=posthog_funnel,
        posthog_sources=posthog_sources,
        runs_daily=runs_daily,
        posthog_funnel_daily=posthog_funnel_daily,
        internal={"int_x"},
        now=NOW,
    )


def test_week_of_is_monday():
    assert growth.week_of("2026-08-26T12:00:00Z") == "2026-08-24"
    assert growth.week_of("2026-08-24T00:00:00+00:00") == "2026-08-24"
    assert growth.week_of(None) is None


def test_segments_exclude_internal_from_external():
    out = growth.derive(**_fixture())
    ext, intl = out["segments"]["external"], out["segments"]["internal"]
    assert ext["owners"]["total"] == 3 and intl["owners"]["total"] == 1
    assert "int_x" not in [o["owner_id"] for o in ext["activation"]["owners"]]


def test_owner_counts_and_signups():
    ext = growth.derive(**_fixture())["segments"]["external"]["owners"]
    assert ext["paid"] == 2 and ext["paid_by_plan"] == {"standard": 1, "ultra_pro": 1}
    assert ext["trial_active"] == 0 and ext["trial_expired"] == 1
    assert ext["paid_by_provider"] == {"chatgpt_oauth": 1, "byo_key": 1}
    assert {"week": "2026-08-17", "n": 2} in ext["signups_by_week"]


def test_activation_stages():
    ext = growth.derive(**_fixture())["segments"]["external"]["activation"]
    by_owner = {o["owner_id"]: o for o in ext["owners"]}
    assert by_owner["ext_a"]["stage"] == "first_autonomous_success"
    assert by_owner["ext_b"]["stage"] == "container_ready"  # kickoff FAILED, and nightly CEO does not count as armed
    assert by_owner["ext_b"]["bootstrap_state"] == "failed"
    assert by_owner["ext_c"]["stage"] == "signed_up"
    assert by_owner["ext_a"]["name"] == "Alpha"
    assert ext["stages"] == {
        "signed_up": 3,
        "subscribed": 2,
        "container_ready": 2,
        "friday_first_run_delivered": 1,
        "automation_armed": 1,
        "first_autonomous_success": 1,
    }
    assert ext["flags"] == {
        "subscribed": 2,
        "container_ready": 2,
        "friday_first_run_delivered": 1,
        "automation_armed": 1,
        "first_autonomous_success": 1,
    }


def test_activation_flags_are_order_independent_of_the_stage_ladder():
    """ext_b's kickoff FAILED, so the ladder stops at container_ready — but if
    ext_b's company later gets an armed routine anyway, the order-independent
    flag must say so even though the ladder still stops short."""
    fx = _fixture()
    fx["routines"].append(
        {
            "company_id": "c_b",
            "id": "r6",
            "title": "Armed despite failed kickoff",
            "status": "active",
            "auto_paused": False,
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
            "has_enabled_trigger": True,
            "trigger_kinds": "schedule",
        }
    )
    fx["first_autonomous"].append({"company_id": "c_b", "first_ok": "2026-08-21T00:00:00Z"})
    ext = growth.derive(**fx)["segments"]["external"]["activation"]
    assert ext["stages"]["automation_armed"] == 1
    assert ext["flags"]["automation_armed"] == 2


def test_org_owner_kickoff_state_comes_from_the_owner_row_regardless_of_scan_order():
    """A DynamoDB scan has no ordering guarantee: an org member's row (no
    bootstrap_state) must never beat the owner's row (has bootstrap_state)
    for that owner's kickoff outcome, no matter which row the scan yields first."""
    owner_row = {
        "user_id": "u_owner",
        "org_id": "int_x",
        "company_id": "c_x",
        "seeded_agent_ids": ["f"],
        "status": "active",
        "bootstrap_state": "delivered",
    }
    member_row = {
        "user_id": "u_member",
        "org_id": "int_x",
        "company_id": "c_x",
        "seeded_agent_ids": ["f"],
        "status": "active",
    }
    for org_rows in ([member_row, owner_row], [owner_row, member_row]):
        fx = _fixture()
        fx["paperclip"] = [r for r in fx["paperclip"] if r.get("org_id") != "int_x"] + org_rows
        internal = growth.derive(**fx)["segments"]["internal"]["activation"]
        by_owner = {o["owner_id"]: o for o in internal["owners"]}
        assert by_owner["int_x"]["bootstrap_state"] == "delivered"
        assert by_owner["int_x"]["stage"] == "first_autonomous_success"


def test_north_star_excludes_nightly_and_headline_four_weeks():
    out = growth.derive(**_fixture())
    ext = out["segments"]["external"]
    last_completed = next(w for w in ext["north_star"]["weeks"] if w["week"] == _week(1))
    assert last_completed == {
        "week": _week(1),
        "owners_active": 1,
        "autonomous_ok": 2,
        "human_ok": 1,
        "nightly_ok": 5,
        "autonomous_share": 2 / 3,
    }
    assert ext["headline"] == {
        "bar": 5,
        "count": 1,
        "owners": ["Alpha"],
        "weeks": [_week(4), _week(3), _week(2), _week(1)],
    }
    assert out["segments"]["internal"]["headline"]["owners"] == ["Lightbulb AI"]


def test_retention_survival_and_depth():
    ext = growth.derive(**_fixture())["segments"]["external"]
    assert ext["retention"]["automation_survival"] == {"armed": 1, "paused_manual": 0, "auto_paused": 1, "archived": 0}
    assert ext["depth"]["armed_per_owner"] == [
        {"owner_id": "ext_a", "name": "Alpha", "armed": 1, "trigger_kinds": ["schedule"]}
    ]
    assert ext["depth"]["providers_per_owner"] == [
        {"owner_id": "ext_a", "name": "Alpha", "providers": ["github", "stripe"]},
        {"owner_id": "ext_b", "name": "Beta", "providers": ["google"]},
    ]


def test_counters_and_nightly():
    ext = growth.derive(**_fixture())["segments"]["external"]
    assert ext["counters"]["auto_paused"] == [
        {"owner_id": "ext_b", "name": "Beta", "title": "Broken", "since": "2026-08-10T00:00:00Z"}
    ]
    assert ext["counters"]["archived_within_7d"] == [{"owner_id": "ext_a", "name": "Alpha", "title": "Old one"}]
    assert ext["nightly_ceo"] == {"armed": 1, "opted_out": 1, "never_armed": 0}


def test_top_level_crosschecks_pass_through():
    out = growth.derive(**_fixture())
    assert out["gate_blocked_7d"] == [{"code": "trial_expired", "n": 7}]
    assert out["gate_blocked_daily"] == [{"day": "2026-08-25", "code": "trial_expired", "n": 7}]
    assert out["posthog"]["runs"] == [
        {"week": _week(1), "kind": "autonomous", "n": 2},
        {"week": _week(1), "kind": "human", "n": 10},
    ]
    assert out["posthog"]["funnel"] == [{"week": _week(1), "event": "trial_started", "owners": 4}]
    assert out["posthog"]["funnel_daily"] == [{"day": "2026-08-25", "event": "trial_started", "owners": 1}]
    assert out["posthog"]["signup_sources"] == [
        {"source": "reddit.com", "owners": 3},
        {"source": "direct", "owners": 2},
    ]
    assert out["window_weeks"] == 8 and out["generated_at"].startswith("2026-08-26")


def test_gate_blocked_daily_passes_through():
    fx = _fixture()
    out = growth.derive(**fx)
    assert out["gate_blocked_7d"] == fx["gate_blocked"]["totals"]
    assert out["gate_blocked_daily"] == fx["gate_blocked"]["daily"]


def test_funnel_daily_excludes_days_outside_the_axis_window():
    """HOGQL_FUNNEL_DAILY's query window (WINDOW_WEEKS+1 weeks) is wider than the
    WINDOW_DAYS axis -- a row outside `days` must be dropped, not leaked through."""
    fx = _fixture()
    out_of_window_day = (NOW.date() - timedelta(days=growth.WINDOW_DAYS + 3)).isoformat()
    fx["posthog_funnel_daily"].append([out_of_window_day, "trial_started", 5])
    out = growth.derive(**fx)
    days = {r["day"] for r in out["posthog"]["funnel_daily"]}
    assert out_of_window_day not in days
    assert "2026-08-25" in days


def test_daily_runs_series_is_continuous_and_segmented():
    fx = _fixture()
    fx["runs_daily"].append({"company_id": "c_x", "day": "2026-08-25", "kind": "autonomous", "ok": 9, "total": 9})
    out = growth.derive(**fx)
    ext_daily = out["segments"]["external"]["daily"]["runs"]
    assert len(ext_daily) == growth.WINDOW_DAYS
    assert [r["day"] for r in ext_daily] == sorted(r["day"] for r in ext_daily)
    by_day = {r["day"]: r for r in ext_daily}
    assert by_day["2026-08-25"]["autonomous_ok"] == 2  # c_x's 9 (internal) must not leak in
    assert by_day["2026-08-24"]["autonomous_ok"] == 1
    other_days = [r for d, r in by_day.items() if d not in ("2026-08-25", "2026-08-24")]
    assert all(r == {"day": r["day"], "autonomous_ok": 0, "human_ok": 0, "nightly_ok": 0} for r in other_days)
    int_by_day = {r["day"]: r for r in out["segments"]["internal"]["daily"]["runs"]}
    assert int_by_day["2026-08-25"]["autonomous_ok"] == 9


def test_daily_signups_bucket_by_day():
    fx = _fixture()
    fx["billing"] = [
        {
            "owner_id": "ext_d",
            "subscription_status": "trial_local",
            "trial_end": 0,
            "created_at": "2026-08-10T00:00:00Z",
        }
    ]
    out = growth.derive(**fx)
    by_day = {r["day"]: r["n"] for r in out["segments"]["external"]["daily"]["signups"]}
    assert by_day["2026-08-10"] == 1
    assert all(n == 0 for d, n in by_day.items() if d != "2026-08-10")


def test_never_armed_counts_paid_owners_with_no_nightly_marker():
    fx = _fixture()
    fx["billing"].append(
        {"owner_id": "ext_d", "subscription_status": "active", "plan": "standard", "created_at": "2026-08-01T00:00:00Z"}
    )
    ext = growth.derive(**fx)["segments"]["external"]
    assert ext["nightly_ceo"] == {"armed": 1, "opted_out": 1, "never_armed": 1}


def test_survival_handles_space_separated_zoneless_timestamp():
    """Data API renders Postgres timestamptz as 'YYYY-MM-DD HH:MM:SS.ffffff' — no zone."""
    fx = _fixture()
    for r in fx["routines"]:
        if r["id"] == "r1":
            r["created_at"] = "2026-06-01 00:00:00.000000"
    out = growth.derive(**fx)
    assert out["segments"]["external"]["retention"]["automation_survival"]["armed"] == 1


def test_archived_routine_with_none_updated_at_is_skipped():
    """A malformed/incomplete row (missing updated_at) must not crash the whole
    computation -- it's simply invisible to the archived-within-7d counter."""
    fx = _fixture()
    fx["routines"].append(
        {
            "company_id": "c_a",
            "id": "r7",
            "title": "No updated_at",
            "status": "archived",
            "auto_paused": False,
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": None,
            "has_enabled_trigger": False,
            "trigger_kinds": None,
        }
    )
    ext = growth.derive(**fx)["segments"]["external"]
    titles = [r["title"] for r in ext["counters"]["archived_within_7d"]]
    assert "No updated_at" not in titles
    assert "Old one" in titles  # the existing archived row still counts


@pytest.mark.asyncio
async def test_compute_partial_when_aurora_fails(monkeypatch):
    growth._cache = None
    fx = _fixture()

    async def boom(*a, **k):
        raise RuntimeError("data api down")

    with (
        patch("core.repositories.billing_repo.scan_all", AsyncMock(return_value=fx["billing"])),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=fx["containers"])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=fx["paperclip"])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=fx["integrations"])),
        patch("core.services.paperclip_sql.select_rows", boom),
        patch(
            "core.services.posthog_admin.hogql",
            AsyncMock(return_value={"columns": [], "results": [], "stubbed": True, "error": None}),
        ),  # all three queries
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value={})),
        patch("core.services.run_analytics.resolve_internal_owner", AsyncMock(side_effect=lambda o: o == "int_x")),
    ):
        data, state, errors = await growth.compute()

    assert state == "partial"
    # stubbed hogql (posthog key/project id unset) fails alongside aurora — a
    # stubbed read must never render as a green, zeroed cross-check.
    assert [e["domain"] for e in errors] == ["aurora", "posthog"]
    assert data["segments"]["external"]["owners"]["total"] == 3
    assert data["segments"]["external"]["activation"]["stages"]["automation_armed"] == 0  # no routine rows


@pytest.mark.asyncio
async def test_compute_posthog_ok_when_not_stubbed():
    """A real (non-stubbed) empty HogQL result is a legitimate zero, not a
    misconfigured key/project id -- posthog must read as ok, not partial."""
    growth._cache = None
    fx = _fixture()

    with (
        patch("core.repositories.billing_repo.scan_all", AsyncMock(return_value=fx["billing"])),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=fx["containers"])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=fx["paperclip"])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=fx["integrations"])),
        patch("core.services.paperclip_sql.select_rows", AsyncMock(return_value=[])),
        patch(
            "core.services.posthog_admin.hogql",
            AsyncMock(return_value={"columns": [], "results": [], "stubbed": False, "error": None}),
        ),
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value={})),
        patch("core.services.run_analytics.resolve_internal_owner", AsyncMock(side_effect=lambda o: o == "int_x")),
    ):
        data, state, errors = await growth.compute()

    assert state == "ok" and errors == []
    assert data["sources"]["posthog"] == "ok"


@pytest.mark.asyncio
async def test_compute_unavailable_when_derive_raises():
    """A malformed row that survives every source read but blows up inside
    derive() must not 500 the endpoint -- same unavailable shape as a DDB-down read."""
    growth._cache = None
    fx = _fixture()

    with (
        patch("core.repositories.billing_repo.scan_all", AsyncMock(return_value=fx["billing"])),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=fx["containers"])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=fx["paperclip"])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=fx["integrations"])),
        patch("core.services.paperclip_sql.select_rows", AsyncMock(return_value=[])),
        patch(
            "core.services.posthog_admin.hogql",
            AsyncMock(return_value={"columns": [], "results": [], "stubbed": False, "error": None}),
        ),
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value={})),
        patch("core.services.run_analytics.resolve_internal_owner", AsyncMock(side_effect=lambda o: o == "int_x")),
        patch("core.services.ops.growth.derive", side_effect=RuntimeError("boom")),
    ):
        data, state, errors = await growth.compute()

    assert data is None and state == "unavailable"
    assert any(e["domain"] == "derive" for e in errors)


@pytest.mark.asyncio
async def test_compute_unavailable_when_clerk_fails():
    """Segmentation is load-bearing (every aggregate assumes it ran before
    aggregation) -- a Clerk failure must fail the whole read, never silently
    classify every owner as external."""
    growth._cache = None
    fx = _fixture()

    async def boom(*a, **k):
        raise RuntimeError("clerk down")

    with (
        patch("core.repositories.billing_repo.scan_all", AsyncMock(return_value=fx["billing"])),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=fx["containers"])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=fx["paperclip"])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=fx["integrations"])),
        patch("core.services.paperclip_sql.select_rows", AsyncMock(return_value=[])),
        patch(
            "core.services.posthog_admin.hogql",
            AsyncMock(return_value={"columns": [], "results": [], "stubbed": True, "error": None}),
        ),
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value={})),
        patch("core.services.run_analytics.resolve_internal_owner", AsyncMock(side_effect=boom)),
    ):
        data, state, errors = await growth.compute()

    assert data is None and state == "unavailable"
    assert [e["domain"] for e in errors] == ["clerk"]


@pytest.mark.asyncio
async def test_compute_unavailable_when_ddb_fails():
    growth._cache = None

    async def boom(*a, **k):
        raise RuntimeError("ddb down")

    with (
        patch("core.repositories.billing_repo.scan_all", boom),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=[])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=[])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=[])),
        patch("core.services.paperclip_sql.select_rows", AsyncMock(return_value=[])),
        patch(
            "core.services.posthog_admin.hogql",
            AsyncMock(return_value={"columns": [], "results": [], "stubbed": True, "error": None}),
        ),
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value={})),
    ):
        data, state, errors = await growth.compute()
    assert data is None and state == "unavailable" and errors[0]["domain"] == "ddb"


@pytest.mark.asyncio
async def test_compute_is_cached():
    growth._cache = (float("inf"), {"cached": True}, "ok", [])
    assert await growth.compute() == ({"cached": True}, "ok", [])
    growth._cache = None
