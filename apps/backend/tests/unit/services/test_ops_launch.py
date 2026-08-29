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
        patch.object(ops_launch.settings, "FRONTEND_URL", "https://dev.flickmeon.ai"),
    ):
        ops_launch._cache.clear()
        yield


async def test_compute_counts_and_economics(deps):
    data, state, errors = await ops_launch.compute()
    assert state == "ok" and errors == []
    assert data["waitlist"] == {"total": 3, "top_slots_activated": 1, "top_slots_claimed": 1}
    # u_a: position 1 ≤ 2 slots and activated inside 72h ⇒ comp-eligible. u_c: position 3 ⇒ paid.
    assert data["entrants"] == {"total": 2, "paid": 1, "comp_eligible": 1, "public": 1}
    assert data["pot_cents"] == 100_000 + 2 * 1_000
    # signups (waitlist) vs competing (entries, joined) vs on_board (entries w/ has_key)
    assert data["signups"] == 3 and data["competing"] == 2 and data["on_board"] == 1
    # public pot tracks the uncapped waitlist; funded pot tracks first-invoice count
    assert data["public_pot_cents"] == 100_000 + 3 * 1_000
    assert data["funded_pot_cents"] == 100_000 + 2 * 1_000
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
    with patch.object(ops_launch.launch_phase, "schedule", return_value=None):
        ops_launch._cache.clear()
        data, state, errors = await ops_launch.compute()
    assert data is None and state == "unavailable" and errors[0]["domain"] == "launch"
