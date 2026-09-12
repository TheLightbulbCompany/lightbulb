"""derive(): the audit §6 metric set over synthetic billing + messaging rows.

Fixture (NOW = Wed 2026-08-26; the current week bucket is 2026-08-24):
  ext_a  paying, signed up 6 weeks ago, activated (5 human msgs over 2 days +
         an agent reply inside its 7-day window), still messaging in week 4
  ext_b  trial, signed up 6 weeks ago, 5 human messages but all on ONE day ->
         NOT activated even though an agent replied
  ext_c  trial (expired), signed up 6 weeks ago, plenty of human messages over
         several days but NO agent message -> NOT activated
  int_x  internal, paying, activated -> must be absent from every external number
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


def _ts(days_back: float) -> str:
    return (NOW - timedelta(days=days_back)).isoformat().replace("+00:00", "Z")


SIGNUP_DAYS_BACK = 41  # 6 weeks minus a day -> cohort week _week(6), window long closed


def _msg(owner, room, kind, sender, at, *, dispatched=False):
    return {
        "pk": f"ROOM#{room}",
        "sk": "MSG#00000000000000000001",
        "ownerId": owner,
        "roomId": room,
        "author": {"kind": kind, "id": sender, "displayName": sender},
        "createdAt": at,
        "dispatchTargets": [{"agentId": "ag1"}] if dispatched else [],
    }


def _human_burst(owner, room, sender, start_days_back, *, n=5, spread_days=2):
    """`n` human messages spread over `spread_days` distinct UTC days."""
    return [_msg(owner, room, "human", sender, _ts(start_days_back - (i % spread_days))) for i in range(n)]


def _fixture():
    billing = [
        {
            "owner_id": "ext_a",
            "subscription_status": "active",
            "plan": "standard",
            "provider_choice": "chatgpt_oauth",
            "created_at": _ts(SIGNUP_DAYS_BACK),
        },
        {
            "owner_id": "ext_b",
            "subscription_status": "trial_local",
            "trial_end": int((NOW + timedelta(days=1)).timestamp()),
            "created_at": _ts(SIGNUP_DAYS_BACK),
        },
        {
            "owner_id": "ext_c",
            "subscription_status": "trial_local",
            "trial_end": 1_700_000_000,
            "created_at": _ts(SIGNUP_DAYS_BACK),
        },
        {
            "owner_id": "int_x",
            "subscription_status": "active",
            "plan": "ultra_pro",
            "provider_choice": "byo_key",
            "created_at": _ts(SIGNUP_DAYS_BACK),
        },
    ]
    messages = []
    # ext_a: activates on day 2 of its window, then keeps messaging in week 4.
    messages += _human_burst("ext_a", "r_a", "u_a", SIGNUP_DAYS_BACK - 1)
    messages.append(_msg("ext_a", "r_a", "agent", "ag1", _ts(SIGNUP_DAYS_BACK - 2)))
    messages.append(_msg("ext_a", "r_a", "human", "u_a", _ts(SIGNUP_DAYS_BACK - 28)))  # week 4 after activation
    # ext_b: five messages, one day only.
    messages += _human_burst("ext_b", "r_b", "u_b", SIGNUP_DAYS_BACK - 1, spread_days=1)
    messages.append(_msg("ext_b", "r_b", "agent", "ag1", _ts(SIGNUP_DAYS_BACK - 2)))
    # ext_c: humans over 3 days, never an agent.
    messages += _human_burst("ext_c", "r_c", "u_c", SIGNUP_DAYS_BACK - 1, n=6, spread_days=3)
    # int_x: activated, must never leak into external.
    messages += _human_burst("int_x", "r_x", "u_x", SIGNUP_DAYS_BACK - 1)
    messages.append(_msg("int_x", "r_x", "agent", "ag1", _ts(SIGNUP_DAYS_BACK - 2)))
    rooms = [
        {"pk": "ROOM#r_a", "sk": "META", "ownerId": "ext_a", "roomId": "r_a"},
        {"pk": "ROOM#r_b", "sk": "META", "ownerId": "ext_b", "roomId": "r_b"},
        {"pk": "ROOM#r_c", "sk": "META", "ownerId": "ext_c", "roomId": "r_c"},
        {"pk": "ROOM#r_x", "sk": "META", "ownerId": "int_x", "roomId": "r_x"},
    ]
    gate_blocked = {
        "totals": [{"code": "trial_expired", "n": 7}],
        "daily": [{"day": "2026-08-25", "code": "trial_expired", "n": 7}],
    }
    return dict(
        billing=billing,
        messages=messages,
        rooms=rooms,
        gate_blocked=gate_blocked,
        dlq_depth=4,
        posthog_funnel=[[_week(1), "trial_started", 4]],
        posthog_sources=[["reddit.com", 3], ["direct", 2]],
        posthog_funnel_daily=[["2026-08-25", "trial_started", 1]],
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
    assert ext["workspaces"]["total"] == 3 and intl["workspaces"]["total"] == 1
    assert ext["messaging"]["rooms"] == 3 and intl["messaging"]["rooms"] == 1
    assert ext["activation"]["activated"] == 1  # ext_a only — int_x is not here
    assert intl["activation"]["activated"] == 1


def test_paying_workspaces_and_signup_cohorts():
    """§6 metric 4: paying is `subscription_status == "active"` — `trialing` is not."""
    fx = _fixture()
    fx["billing"].append(
        {"owner_id": "ext_d", "subscription_status": "trialing", "plan": "standard", "created_at": _ts(2)}
    )
    ext = growth.derive(**fx)["segments"]["external"]
    assert ext["workspaces"]["paying"] == 1
    assert ext["workspaces"]["paying_by_plan"] == {"standard": 1}
    assert ext["workspaces"]["trial_active"] == 1 and ext["workspaces"]["trial_expired"] == 1
    cohort = next(c for c in ext["cohorts"] if c["week"] == _week(6))
    assert cohort == {"week": _week(6), "signups": 3, "measurable": 3, "activated": 1, "paying": 1}


def test_activation_needs_both_conditions():
    """Five messages on one day is not activation; messages without an agent
    reply is not activation. Both conditions, or the workspace is not activated."""
    ext = growth.derive(**_fixture())["segments"]["external"]["activation"]
    assert ext == {"activated": 1, "measurable": 3, "rate": 1 / 3}


def test_activation_is_workspace_scoped_not_per_sender():
    """Controller ruling: the workspace's humans count TOGETHER. Two people
    sending three messages each across two days activates it; one person
    sending four does not, however the messages are spread."""
    fx = _fixture()
    fx["billing"] = [
        {"owner_id": "ext_two", "subscription_status": "trial_local", "created_at": _ts(SIGNUP_DAYS_BACK)},
        {"owner_id": "ext_one", "subscription_status": "trial_local", "created_at": _ts(SIGNUP_DAYS_BACK)},
    ]
    day_a, day_b = SIGNUP_DAYS_BACK - 1, SIGNUP_DAYS_BACK - 2
    fx["messages"] = [
        # ext_two: 3 + 3 from two humans, over two distinct UTC days.
        *(_msg("ext_two", "r2", "human", "u_1", _ts(d)) for d in (day_a, day_a, day_b)),
        *(_msg("ext_two", "r2", "human", "u_2", _ts(d)) for d in (day_a, day_b, day_b)),
        _msg("ext_two", "r2", "agent", "ag1", _ts(day_b)),
        # ext_one: a single human, four messages, two days — one short.
        *(_msg("ext_one", "r1", "human", "u_3", _ts(d)) for d in (day_a, day_a, day_b, day_b)),
        _msg("ext_one", "r1", "agent", "ag1", _ts(day_b)),
    ]
    seg = growth.derive(**fx)["segments"]["external"]
    assert seg["activation"] == {"activated": 1, "measurable": 2, "rate": 0.5}
    by_week = {c["week"]: c for c in seg["cohorts"]}
    assert by_week[_week(6)]["activated"] == 1


def test_activation_ignores_messages_outside_the_seven_day_window():
    fx = _fixture()
    # Push ext_b's agent reply 10 days past signup: the human side still fails
    # (one day), so move its burst to 2 days as well and keep only the agent late.
    fx["messages"] = [m for m in fx["messages"] if m["ownerId"] != "ext_b"]
    fx["messages"] += _human_burst("ext_b", "r_b", "u_b", SIGNUP_DAYS_BACK - 1)
    fx["messages"].append(_msg("ext_b", "r_b", "agent", "ag1", _ts(SIGNUP_DAYS_BACK - 10)))
    assert growth.derive(**fx)["segments"]["external"]["activation"]["activated"] == 1
    # Same agent reply inside the window activates it.
    fx["messages"][-1] = _msg("ext_b", "r_b", "agent", "ag1", _ts(SIGNUP_DAYS_BACK - 6))
    assert growth.derive(**fx)["segments"]["external"]["activation"]["activated"] == 2


def test_activation_denominator_excludes_an_open_window():
    """A workspace that signed up yesterday has not failed activation yet."""
    fx = _fixture()
    fx["billing"].append({"owner_id": "ext_new", "subscription_status": "trial_local", "created_at": _ts(1)})
    ext = growth.derive(**fx)["segments"]["external"]["activation"]
    assert ext["measurable"] == 3 and ext["rate"] == 1 / 3


def test_weekly_active_and_multiplayer_share():
    fx = _fixture()
    # This week: ext_a has two distinct human senders, ext_b has one.
    fx["messages"] += [
        _msg("ext_a", "r_a", "human", "u_a", _ts(0.5)),
        _msg("ext_a", "r_a", "human", "u_a2", _ts(0.4)),
        _msg("ext_b", "r_b", "human", "u_b", _ts(0.3)),
        _msg("ext_a", "r_a", "agent", "ag1", _ts(0.2)),  # agents never make a week "active"
    ]
    weeks = {w["week"]: w for w in growth.derive(**fx)["segments"]["external"]["weekly_active"]}
    assert weeks[_week(0)] == {"week": _week(0), "workspaces": 2, "multiplayer": 1, "multiplayer_share": 0.5}
    assert weeks[_week(1)] == {"week": _week(1), "workspaces": 0, "multiplayer": 0, "multiplayer_share": None}


def test_weekly_active_respects_the_utc_week_boundary():
    """Monday 00:00Z opens a new bucket; the Sunday before it does not."""
    fx = _fixture()
    fx["messages"] = [
        _msg("ext_a", "r_a", "human", "u_a", "2026-08-23T23:59:59Z"),  # Sunday -> week 2026-08-17
        _msg("ext_b", "r_b", "human", "u_b", "2026-08-24T00:00:00Z"),  # Monday -> week 2026-08-24
    ]
    weeks = {w["week"]: w["workspaces"] for w in growth.derive(**fx)["segments"]["external"]["weekly_active"]}
    assert weeks["2026-08-17"] == 1 and weeks["2026-08-24"] == 1


def test_answered_rate_and_p50_over_the_120s_window():
    fx = _fixture()
    base = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

    def at(seconds):
        return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    fx["messages"] = [
        # answered in 30s
        _msg("ext_a", "r1", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r1", "agent", "ag1", at(30)),
        # answered exactly at the boundary
        _msg("ext_a", "r2", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r2", "agent", "ag1", at(120)),
        # one second late -> not answered
        _msg("ext_a", "r3", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r3", "agent", "ag1", at(121)),
        # an agent reply in ANOTHER room does not answer this one
        _msg("ext_a", "r4", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r5", "agent", "ag1", at(5)),
        # no dispatch target -> not counted at all
        _msg("ext_a", "r6", "human", "u_a", at(0)),
    ]
    ext = growth.derive(**fx)["segments"]["external"]["answered"]
    assert ext == {"dispatched": 4, "answered": 2, "rate": 0.5, "p50_reply_s": 75.0}


def test_answered_ignores_an_agent_message_that_precedes_the_human():
    """An agent turn already in the room before the request is not its reply."""
    fx = _fixture()
    base = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

    def at(seconds):
        return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    fx["messages"] = [
        _msg("ext_a", "r1", "agent", "ag1", at(-30)),
        _msg("ext_a", "r1", "human", "u_a", at(0), dispatched=True),
    ]
    assert growth.derive(**fx)["segments"]["external"]["answered"] == {
        "dispatched": 1,
        "answered": 0,
        "rate": 0.0,
        "p50_reply_s": None,
    }


def test_answered_measures_the_next_agent_message_not_a_later_one():
    """Two agent turns follow the request: the latency is the FIRST one's. A
    later reply must not be able to rescue a request the next one already missed."""
    fx = _fixture()
    base = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

    def at(seconds):
        return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    fx["messages"] = [
        _msg("ext_a", "r1", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r1", "agent", "ag1", at(20)),
        _msg("ext_a", "r1", "agent", "ag1", at(300)),
        # r2: the next agent message is late, and a much later one cannot save it.
        _msg("ext_a", "r2", "human", "u_a", at(0), dispatched=True),
        _msg("ext_a", "r2", "agent", "ag1", at(200)),
        _msg("ext_a", "r2", "agent", "ag1", at(400)),
    ]
    assert growth.derive(**fx)["segments"]["external"]["answered"] == {
        "dispatched": 2,
        "answered": 1,
        "rate": 0.5,
        "p50_reply_s": 20.0,
    }


def test_answered_is_zero_not_none_when_nothing_was_dispatched():
    fx = _fixture()
    fx["messages"] = [_msg("ext_a", "r1", "human", "u_a", _ts(1))]
    ext = growth.derive(**fx)["segments"]["external"]["answered"]
    assert ext == {"dispatched": 0, "answered": 0, "rate": None, "p50_reply_s": None}


def test_week4_retention_counts_only_completed_fourth_weeks():
    out = growth.derive(**_fixture())["segments"]["external"]["retention_w4"]
    assert out == {"eligible": 1, "retained": 1, "rate": 1.0}


def test_week4_retention_misses_a_silent_workspace():
    fx = _fixture()
    fx["messages"] = [m for m in fx["messages"] if m["createdAt"] != _ts(SIGNUP_DAYS_BACK - 28)]
    assert growth.derive(**fx)["segments"]["external"]["retention_w4"] == {
        "eligible": 1,
        "retained": 0,
        "rate": 0.0,
    }


def test_week4_retention_skips_an_activation_too_recent_to_judge():
    fx = _fixture()
    fx["billing"] = [{"owner_id": "ext_n", "subscription_status": "trial_local", "created_at": _ts(8)}]
    fx["messages"] = _human_burst("ext_n", "r_n", "u_n", 7) + [_msg("ext_n", "r_n", "agent", "ag1", _ts(6))]
    seg = growth.derive(**fx)["segments"]["external"]
    assert seg["activation"]["activated"] == 1
    assert seg["retention_w4"] == {"eligible": 0, "retained": 0, "rate": None}


def test_messaging_card_counts_rooms_and_human_messages_this_week():
    fx = _fixture()
    fx["messages"] += [
        _msg("ext_a", "r_a", "human", "u_a", _ts(0.5)),
        _msg("ext_a", "r_a", "agent", "ag1", _ts(0.4)),  # agents are not counted
    ]
    ext = growth.derive(**fx)["segments"]["external"]["messaging"]
    assert ext == {"rooms": 3, "human_messages_this_week": 1}


def test_daily_series_are_continuous_and_segmented():
    out = growth.derive(**_fixture())
    daily = out["segments"]["external"]["daily"]
    for key in ("signups", "human_messages", "activated"):
        assert len(daily[key]) == growth.WINDOW_DAYS
        assert [r["day"] for r in daily[key]] == sorted(r["day"] for r in daily[key])
    assert sum(r["n"] for r in daily["signups"]) == 3
    assert sum(r["n"] for r in daily["activated"]) == 1
    assert sum(r["n"] for r in out["segments"]["internal"]["daily"]["activated"]) == 1


def test_malformed_message_rows_are_skipped_not_fatal():
    fx = _fixture()
    fx["messages"] += [
        {"ownerId": "ext_a", "roomId": "r_a", "author": {"kind": "human", "id": "u"}},  # no createdAt
        {"ownerId": "ext_a", "roomId": "r_a", "createdAt": _ts(1)},  # no author
        _msg("ext_a", "r_a", "system", "sys", _ts(0.5)),  # system is neither human nor agent
        _msg("ext_a", None, "human", "u_a", _ts(0.5)),  # no room
    ]
    ext = growth.derive(**fx)["segments"]["external"]
    assert ext["messaging"]["human_messages_this_week"] == 0


def test_top_level_crosschecks_pass_through():
    out = growth.derive(**_fixture())
    assert out["gate_blocked_7d"] == [{"code": "trial_expired", "n": 7}]
    assert out["gate_blocked_daily"] == [{"day": "2026-08-25", "code": "trial_expired", "n": 7}]
    assert out["dlq_depth"] == 4
    assert out["posthog"]["funnel"] == [{"week": _week(1), "event": "trial_started", "owners": 4}]
    assert out["posthog"]["funnel_daily"] == [{"day": "2026-08-25", "event": "trial_started", "owners": 1}]
    assert out["posthog"]["signup_sources"] == [
        {"source": "reddit.com", "owners": 3},
        {"source": "direct", "owners": 2},
    ]
    assert out["window_weeks"] == 8 and out["generated_at"].startswith("2026-08-26")


def test_funnel_daily_excludes_days_outside_the_axis_window():
    """HOGQL_FUNNEL_DAILY's query window (WINDOW_WEEKS+1 weeks) is wider than the
    WINDOW_DAYS axis -- a row outside `days` must be dropped, not leaked through."""
    fx = _fixture()
    out_of_window_day = (NOW.date() - timedelta(days=growth.WINDOW_DAYS + 3)).isoformat()
    fx["posthog_funnel_daily"].append([out_of_window_day, "trial_started", 5])
    days = {r["day"] for r in growth.derive(**fx)["posthog"]["funnel_daily"]}
    assert out_of_window_day not in days and "2026-08-25" in days


def _patched_sources(fx, **overrides):
    reads = {
        "core.repositories.billing_repo.scan_all": AsyncMock(return_value=fx["billing"]),
        "core.repositories.messaging_repo.scan_growth_rows": AsyncMock(return_value=(fx["messages"], fx["rooms"])),
        "core.services.posthog_admin.hogql": AsyncMock(
            return_value={"columns": [], "results": [], "stubbed": False, "error": None}
        ),
        "core.services.ops.gate_blocked.last_7d": AsyncMock(return_value={}),
        "core.services.ops.dlq.work_dlq_depth": AsyncMock(return_value=0),
        "core.services.run_analytics.resolve_internal_owner": AsyncMock(side_effect=lambda o: o == "int_x"),
    }
    reads.update(overrides)
    return [patch(target, value) for target, value in reads.items()]


async def _compute_with(fx, **overrides):
    growth._cache = None
    patches = _patched_sources(fx, **overrides)
    for p in patches:
        p.start()
    try:
        return await growth.compute()
    finally:
        for p in patches:
            p.stop()
        growth._cache = None


@pytest.mark.asyncio
async def test_compute_ok_reads_every_source():
    data, state, errors = await _compute_with(_fixture())
    assert state == "ok" and errors == []
    assert data["sources"] == {
        "ddb": "ok",
        "messaging": "ok",
        "posthog": "ok",
        "cloudwatch": "ok",
        "clerk": "ok",
    }
    assert data["segments"]["external"]["workspaces"]["total"] == 3
    assert data["segments"]["external"]["activation"]["activated"] == 1


@pytest.mark.asyncio
async def test_compute_partial_when_the_messaging_scan_fails():
    """A failed messaging read must surface as partial, never as a zeroed,
    green activation/answered panel."""

    async def boom(*a, **k):
        raise RuntimeError("scan throttled")

    data, state, errors = await _compute_with(_fixture(), **{"core.repositories.messaging_repo.scan_growth_rows": boom})
    assert state == "partial" and [e["domain"] for e in errors] == ["messaging"]
    assert data["sources"]["messaging"] == "unavailable"

    ext = data["segments"]["external"]
    # Every messaging-fed figure is None so the page renders an em dash / a gap.
    # A green 0% here would claim "nobody activated", which we do not know.
    assert ext["activation"] == {"activated": None, "measurable": None, "rate": None}
    assert ext["answered"] == {"dispatched": None, "answered": None, "rate": None, "p50_reply_s": None}
    assert ext["retention_w4"] == {"eligible": None, "retained": None, "rate": None}
    assert ext["messaging"] == {"rooms": None, "human_messages_this_week": None}
    assert all(
        w["workspaces"] is None and w["multiplayer"] is None and w["multiplayer_share"] is None
        for w in ext["weekly_active"]
    )
    assert all(c["measurable"] is None and c["activated"] is None for c in ext["cohorts"])
    assert all(d["n"] is None for d in ext["daily"]["human_messages"] + ext["daily"]["activated"])
    # Billing-fed figures survive — that read succeeded. (compute() derives on the
    # real clock, so the fixture's 6-week-old signups fall outside today's axis;
    # what matters is that these stay numbers rather than being blanked too.)
    assert ext["workspaces"]["total"] == 3 and ext["workspaces"]["paying"] == 1
    assert all(isinstance(c["signups"], int) and isinstance(c["paying"], int) for c in ext["cohorts"])
    assert all(isinstance(d["n"], int) for d in ext["daily"]["signups"])


@pytest.mark.asyncio
async def test_compute_partial_when_the_dlq_read_fails():
    """Both CloudWatch reads share one source key — either failing marks it."""

    async def boom(*a, **k):
        raise RuntimeError("access denied")

    data, state, errors = await _compute_with(_fixture(), **{"core.services.ops.dlq.work_dlq_depth": boom})
    assert state == "partial" and [e["domain"] for e in errors] == ["cloudwatch"]
    assert data["dlq_depth"] is None


@pytest.mark.asyncio
async def test_compute_partial_when_posthog_is_stubbed():
    """A stubbed read (key/project id unset) must never render as a green zero."""
    data, state, errors = await _compute_with(
        _fixture(),
        **{
            "core.services.posthog_admin.hogql": AsyncMock(
                return_value={"columns": [], "results": [], "stubbed": True, "error": None}
            )
        },
    )
    assert state == "partial" and [e["domain"] for e in errors] == ["posthog"]
    assert data["sources"]["posthog"] == "unavailable"


@pytest.mark.asyncio
async def test_compute_unavailable_when_clerk_fails():
    """Segmentation is load-bearing -- a Clerk failure must fail the whole read,
    never silently classify every workspace as external."""

    async def boom(*a, **k):
        raise RuntimeError("clerk down")

    data, state, errors = await _compute_with(
        _fixture(), **{"core.services.run_analytics.resolve_internal_owner": boom}
    )
    assert data is None and state == "unavailable"
    assert [e["domain"] for e in errors] == ["clerk"]


@pytest.mark.asyncio
async def test_compute_unavailable_when_ddb_fails():
    async def boom(*a, **k):
        raise RuntimeError("ddb down")

    data, state, errors = await _compute_with(_fixture(), **{"core.repositories.billing_repo.scan_all": boom})
    assert data is None and state == "unavailable" and errors[0]["domain"] == "ddb"


@pytest.mark.asyncio
async def test_compute_unavailable_when_derive_raises():
    """A malformed row that survives every source read but blows up inside
    derive() must not 500 the endpoint -- same unavailable shape as a DDB-down read."""

    def explode(**_kwargs):
        raise RuntimeError("boom")

    data, state, errors = await _compute_with(_fixture(), **{"core.services.ops.growth.derive": explode})
    assert data is None and state == "unavailable"
    assert any(e["domain"] == "derive" for e in errors)


@pytest.mark.asyncio
async def test_compute_is_cached():
    growth._cache = (float("inf"), {"cached": True}, "ok", [])
    assert await growth.compute() == ({"cached": True}, "ok", [])
    growth._cache = None
