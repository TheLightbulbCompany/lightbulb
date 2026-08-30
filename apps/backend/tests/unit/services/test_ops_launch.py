from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_phase
from core.services.ops import launch as ops_launch

DROP = datetime(2026, 9, 18, 16, tzinfo=timezone.utc)
S = launch_phase.Schedule(
    DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), 2, 72, 30
)  # 2 comp slots for the test


@pytest.fixture
def deps():
    waitlist = [
        {
            "email": "a@x.com",
            "position": 1,
            "referral_code": "AAAA2345",
            "user_id": "u_a",
            "created_at": "t",
            "referrals_signed_up": 1,
            "referrals_paid": 1,
            "free_months_banked": 0,
        },
        {
            "email": "b@x.com",
            "position": 2,
            "referral_code": "BBBB2345",
            "user_id": None,
            "created_at": "t",
            "referrals_signed_up": 0,
            "referrals_paid": 0,
            "free_months_banked": 1,
        },
        {
            "email": "c@x.com",
            "position": 3,
            "referral_code": "CCCC2345",
            "user_id": "u_c",
            "created_at": "t",
            "referred_by": "AAAA2345",
            "referrals_signed_up": 0,
            "referrals_paid": 0,
            "free_months_banked": 0,
        },
    ]
    entries = [
        {
            "owner_id": "u_a",
            "display_name": "A",
            "public": True,
            "has_key": True,
            "waitlist_position": 1,
            "currency": "usd",
            "baseline_cents": 400,
            "baseline_prorated_cents": 100,
            "window_cents": 300,
            "delta_cents": 200,
            "refreshed_at": "t",
        },
        {
            "owner_id": "u_c",
            "display_name": "C",
            "public": False,
            "has_key": False,
            "waitlist_position": 3,
            "currency": "usd",
            "baseline_cents": 0,
            "window_cents": 50,
            "delta_cents": 50,
            "refresh_error": "stripe_auth",
        },
    ]
    onboarding = {
        "u_a": {"bootstrap_state": "delivered", "bootstrap_delivered_at": (DROP + timedelta(hours=1)).isoformat()},
        "u_c": {"bootstrap_state": "delivered", "bootstrap_delivered_at": (DROP + timedelta(hours=100)).isoformat()},
    }
    with (
        patch.object(ops_launch.launch_phase, "schedule", return_value=S),
        patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(return_value=waitlist)),
        patch.object(ops_launch.launch_waitlist_repo, "count_pot_counted", AsyncMock(return_value=2)),
        patch.object(ops_launch.launch_waitlist_repo, "count", AsyncMock(return_value=3)),
        patch.object(ops_launch.launch_entries_repo, "list_all", AsyncMock(return_value=entries)),
        patch.object(
            ops_launch.readiness_state,
            "resolve_onboarding_state",
            AsyncMock(side_effect=lambda o: onboarding.get(o, {"bootstrap_state": "not_started"})),
        ),
        patch.object(
            ops_launch.billing_repo,
            "get_by_owner_id",
            AsyncMock(side_effect=lambda o: {"stripe_subscription_id": f"sub_{o}"}),
        ),
        patch.object(ops_launch.launch_service.settings, "FRONTEND_URL", "https://dev.flickmeon.ai"),
        patch.object(
            ops_launch.launch_settings,
            "effective",
            AsyncMock(return_value=dict(ops_launch.launch_settings.DEFAULTS, enabled=True)),
        ),
    ):
        ops_launch._cache.clear()
        yield


async def test_compute_counts_and_economics(deps):
    data, state, errors = await ops_launch.compute()
    assert state == "ok" and errors == []
    # The admin page branches on `data.enabled` uniformly whether or not the
    # sheet itself is computable — see the disabled-branch equivalent in
    # test_compute_unconfigured below.
    assert data["enabled"] is True
    expected_settings = {k: v for k, v in ops_launch.launch_settings.DEFAULTS.items() if k != "mint_nonce"}
    expected_settings["enabled"] = True  # the `deps` fixture's mocked effective() row
    assert data["settings"] == expected_settings
    assert data["waitlist"] == {"total": 3, "top_slots_activated": 1, "top_slots_claimed": 1}
    # u_a: position 1 ≤ 2 slots and activated inside 72h ⇒ comp-eligible. u_c: position 3 ⇒ paid.
    assert data["competing_breakdown"] == {"paid": 1, "comp_eligible": 1, "public": 1}
    # signups (waitlist) vs competing (entries, joined) vs on_board (entries w/ has_key)
    assert data["signups"] == 3 and data["competing"] == 2 and data["on_board"] == 1
    # public pot tracks the uncapped waitlist; funded pot tracks first-invoice count
    assert data["public_pot_cents"] == 100_000 + 3 * 1_000
    assert data["funded_pot_cents"] == 100_000 + 2 * 1_000
    assert data["pot_counted"] == 2
    assert data["referrals"] == {"signed_up": 1, "paid": 1, "months_banked": 1}
    econ = data["economics"]
    assert econ["paid_profit_cents"] == 1 * (5000 - 1800 - 1000)
    assert econ["comp_cost_cents"] == 1 * (1800 + 1000)
    assert econ["referral_cost_cents"] == 1 * (1800 + 1000)
    assert econ["net_cents"] == econ["pnl_cents"] + 500_000
    assert (
        data["entries"][0]["owner_id"] == "u_a"
        and data["entries"][0]["pct"] == 2.0  # 200 / prorated 100, not / baseline 400
        and data["entries"][0]["comp_eligible"] is True
    )
    assert data["entries"][1]["refresh_error"] == "stripe_auth"
    row = next(r for r in data["waitlist_rows"] if r["email"] == "a@x.com")
    assert row["referral_link"] == "https://dev.flickmeon.ai/?ref=AAAA2345" and row["activated"] is True
    # An unclaimed row is definitively not activated — never an unknown.
    assert next(r for r in data["waitlist_rows"] if r["email"] == "b@x.com")["activated"] is False
    # The pot formula's own inputs ride along so the console renders the sum.
    assert data["pot_front_cents"] == 100_000 and data["pot_per_entrant_cents"] == 1_000


async def test_signups_comes_from_the_counter_not_the_scan(deps):
    """Same source as GET /launch — the atomic position counter."""
    with patch.object(ops_launch.launch_waitlist_repo, "count", AsyncMock(return_value=41)):
        ops_launch._cache.clear()
        data, _, _ = await ops_launch.compute()
    assert data["signups"] == 41 and data["waitlist"]["total"] == 3
    assert data["public_pot_cents"] == 100_000 + 41 * 1_000


async def test_onboarding_fanout_is_bounded_to_comped_slots_and_entrants():
    """An uncapped waitlist must not mean one onboarding read per signup."""
    slots = 220
    sched = launch_phase.Schedule(DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), slots, 72, 30)
    waitlist = [
        {"email": f"u{i}@x.com", "position": i, "referral_code": f"C{i:07d}", "user_id": f"u_{i}", "created_at": "t"}
        for i in range(1, 501)
    ]
    entries = [
        {"owner_id": f"u_{i}", "display_name": f"E{i}", "public": True, "has_key": False, "waitlist_position": i}
        for i in (400, 401, 402)  # entrants who sat OUTSIDE the comped top slots
    ]
    calls: list[str] = []

    async def _onb(owner_id: str) -> dict:
        calls.append(owner_id)
        return {"bootstrap_state": "not_started"}

    with (
        patch.object(ops_launch.launch_phase, "schedule", return_value=sched),
        patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(return_value=waitlist)),
        patch.object(ops_launch.launch_waitlist_repo, "count_pot_counted", AsyncMock(return_value=0)),
        patch.object(ops_launch.launch_waitlist_repo, "count", AsyncMock(return_value=500)),
        patch.object(ops_launch.launch_entries_repo, "list_all", AsyncMock(return_value=entries)),
        patch.object(ops_launch.readiness_state, "resolve_onboarding_state", AsyncMock(side_effect=_onb)),
        patch.object(ops_launch.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        patch.object(ops_launch.launch_service.settings, "FRONTEND_URL", "https://dev.flickmeon.ai"),
        patch.object(
            ops_launch.launch_settings, "effective", AsyncMock(return_value=dict(ops_launch.launch_settings.DEFAULTS))
        ),
    ):
        ops_launch._cache.clear()
        data, state, _ = await ops_launch.compute()

    assert state == "ok"
    # top 220 positions ∪ 3 entry owners (all outside the top) = 223, never 500.
    assert len(calls) <= slots + 3 and len(calls) == 223
    assert data["signups"] == 500 and data["competing"] == 3
    # Rows nobody asked about report null, not a fabricated False.
    assert next(r for r in data["waitlist_rows"] if r["position"] == 1)["activated"] is False
    assert next(r for r in data["waitlist_rows"] if r["position"] == 300)["activated"] is None
    assert next(r for r in data["waitlist_rows"] if r["position"] == 400)["activated"] is False


async def test_compute_is_cached_for_60s(deps):
    first = await ops_launch.compute()
    assert first[1] == "ok"
    with patch.object(
        ops_launch.launch_waitlist_repo, "list_all", AsyncMock(side_effect=AssertionError("should be cached"))
    ):
        second = await ops_launch.compute()
    assert second is first  # the cached tuple, not a re-read that read_source turned into UNAVAILABLE
    assert second[1] == "ok"


async def test_compute_unconfigured():
    # Feature off ⇒ no economics to compute, but the admin console still gets
    # the settings row + enabled flag so it can render the settings card and
    # let an admin turn the feature on, instead of the page going blank.
    with (
        patch.object(ops_launch.launch_phase, "schedule", return_value=None),
        patch.object(ops_launch.launch_settings.launch_settings_repo, "get", AsyncMock(return_value=None)),
    ):
        ops_launch._cache.clear()
        data, state, errors = await ops_launch.compute()
    assert state == "unavailable" and errors[0]["domain"] == "launch"
    # to_public strips mint_nonce even here — it's never a client-visible field.
    expected_settings = {k: v for k, v in ops_launch.launch_settings.DEFAULTS.items() if k != "mint_nonce"}
    assert data == {"settings": expected_settings, "enabled": False}


async def test_compute_keeps_the_settings_shell_when_the_sheet_read_fails(deps):
    """A failed/timed-out sheet read still has to carry the settings row: the
    admin page renders the settings card off `data`, so a bare None would take
    away the only control that can turn the competition back off."""
    with patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(side_effect=RuntimeError("ddb down"))):
        ops_launch._cache.clear()
        data, state, errors = await ops_launch.compute()
    assert state == "unavailable" and errors[0]["domain"] == "launch"
    expected_settings = {k: v for k, v in ops_launch.launch_settings.DEFAULTS.items() if k != "mint_nonce"}
    expected_settings["enabled"] = True  # the `deps` fixture's mocked effective() row
    assert data == {"settings": expected_settings, "enabled": True}
    assert ops_launch._cache == {}  # a failed read is never cached


async def test_compute_keeps_the_settings_shell_when_the_sheet_read_times_out(deps):
    """Same for a timeout — every non-ok state keeps the shell, not just the
    exception one."""
    timed_out = (None, "unavailable", [{"domain": "launch", "reason": "timeout after 20.0s"}])
    with patch.object(ops_launch.env, "read_source", AsyncMock(return_value=timed_out)):
        ops_launch._cache.clear()
        data, state, errors = await ops_launch.compute()
    assert state == "unavailable" and "timeout" in errors[0]["reason"]
    assert data["enabled"] is True and data["settings"]["comp_slots"] == 220
