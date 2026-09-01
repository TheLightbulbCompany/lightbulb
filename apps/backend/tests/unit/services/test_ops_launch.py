from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import launch_settings_repo
from core.services import launch_phase
from core.services.ops import launch as ops_launch

CLOSES = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
S = launch_phase.Schedule(CLOSES, CLOSES + timedelta(hours=12), CLOSES + timedelta(days=61), 2, None)


def _settings_row(**over):
    row = dict(launch_settings_repo.DEFAULTS, enabled=True)
    row.update(over)
    return row


@pytest.fixture
def deps():
    waitlist = [
        {
            "email": "a@x.com",
            "position": 1,
            "referral_code": "AAAA2345",
            "user_id": "u_a",
            "created_at": "t",
            "joined_at": "t",
            "display_name": "A",
            "referrals_signed_up": 1,
            "referrals_subscribed": 1,
            "winner": True,
            "compete_eligible": True,
            "coupon_attached_at": "t",
        },
        {
            "email": "b@x.com",
            "position": 2,
            "referral_code": "BBBB2345",
            "created_at": "t",
            # No joined_at — an attribution-created row, not sheet membership.
            "referrals_signed_up": 0,
            "referrals_subscribed": 0,
        },
        {
            "email": "c@x.com",
            "position": 30,
            "referral_code": "CCCC2345",
            "user_id": "u_c",
            "created_at": "t",
            "joined_at": "t",
            "referred_by": "AAAA2345",
            "referrals_signed_up": 0,
            "referrals_subscribed": 2,
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
    teams = [
        {
            "owner_id": "org_t",
            "team_name": "Tuple",
            "seats": 4,
            "note": "six people, four seats",
            "submitter_email": "founder@tuple.com",
            "submitter_user_id": "u_t",
            "submitter_org_slug": "tuple",
            "counted": True,
            "submitted_at": "t",
            "updated_at": "t9",
        },
        {
            "owner_id": "org_u",
            "team_name": "Unreviewed",
            "seats": 9,
            "counted": False,
            "submitted_at": "t2",
        },
    ]
    billing = {"u_a": {"stripe_subscription_id": "sub_a"}}
    settings_row = _settings_row()
    with (
        patch.object(ops_launch.launch_phase, "schedule", return_value=S),
        patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(return_value=waitlist)),
        patch.object(ops_launch.launch_waitlist_repo, "count", AsyncMock(return_value=3)),
        patch.object(ops_launch.launch_entries_repo, "list_all", AsyncMock(return_value=entries)),
        patch.object(ops_launch.launch_teams_repo, "list_all", AsyncMock(return_value=teams)),
        patch.object(ops_launch.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda o: billing.get(o))),
        patch.object(ops_launch.launch_settings, "effective", AsyncMock(return_value=settings_row)),
    ):
        ops_launch.clear_cache()
        yield settings_row
        ops_launch.clear_cache()


async def test_sheet_payload_shape(deps):
    data, state, errors = await ops_launch.compute()
    assert state == "ok" and errors == []
    assert data["enabled"] is True
    assert data["schedule"]["sheet_closes_at"] == CLOSES.isoformat()
    assert data["schedule"]["comp_slots"] == 2
    assert data["signups"] == 3
    assert data["sheet"] == {"total": 3, "joined": 2, "winners": 1}
    assert data["competing"] == 2 and data["on_board"] == 1

    rows = data["sheet_rows"]
    assert [r["email"] for r in rows] == ["a@x.com", "c@x.com"]  # joined only, by effective position
    a, c = rows
    assert a["winner"] is True and a["compete_eligible"] is True and a["coupon_attached_at"] == "t"
    assert a["referrals_subscribed"] == 1
    # c: position 30, 2 subscribed referrals × jump 10 -> effective 10.
    assert c["effective_position"] == 10
    assert c["winner"] is False and c["compete_eligible"] is True  # pre-close default
    assert c["referred_by"] == "AAAA2345"

    # Team entries ride along too — both of them, counted or not, so an
    # admin can review what is waiting.
    assert data["teams"] == {"total": 2, "counted": 1, "contribution_cents": 1_000 + 4 * 500}
    t_counted, t_pending = data["team_entries"]
    assert t_counted["team_name"] == "Tuple" and t_counted["counted"] is True
    assert t_counted["contribution_cents"] == 1_000 + 4 * 500
    # An edit after review keeps submitted_at, so updated_at is the only
    # column that can show it happened.
    assert t_counted["submitted_at"] == "t" and t_counted["updated_at"] == "t9"
    assert t_pending["counted"] is False and t_pending["contribution_cents"] == 1_000 + 9 * 500

    # Board section still rides along, keyed by entries.
    assert data["entries"][0]["owner_id"] == "u_a" and data["entries"][0]["has_subscription"] is True


async def test_pot_is_admin_only_and_projected_pre_close(deps):
    data, _, _ = await ops_launch.compute()
    assert data["pot_front_cents"] == 100_000 and data["pot_per_entrant_cents"] == 1_000
    assert data["pot_per_seat_cents"] == 500
    # 2 joined individuals + ONE counted team (4 seats): the uncounted 9-seat
    # submission contributes nothing.
    assert data["projected_pot_cents"] == 100_000 + 2 * 1_000 + (1_000 + 4 * 500)
    assert data["closed"] is False and data["closed_at"] is None
    assert data["final_pot_cents"] is None and data["final_roster_count"] is None


async def test_close_state_surfaces_verbatim(deps):
    deps.update(closed_at="2026-09-30T16:00:00+00:00", final_pot_cents=103_000, final_roster_count=3)
    data, _, _ = await ops_launch.compute()
    assert data["closed"] is True and data["closed_at"] == "2026-09-30T16:00:00+00:00"
    assert data["final_pot_cents"] == 103_000 and data["final_roster_count"] == 3


async def test_disabled_env_still_returns_the_settings_card(deps):
    with patch.object(ops_launch.launch_phase, "schedule", return_value=None):
        data, state, errors = await ops_launch.compute()
    assert state == "unavailable" and data["enabled"] is False
    assert "settings" in data and "mint_nonce" not in data["settings"]
    assert errors and errors[0]["domain"] == "launch"


async def test_compute_caches_for_60s(deps):
    first, _, _ = await ops_launch.compute()
    second, _, _ = await ops_launch.compute()
    assert first is second  # same cached object


async def test_async_close_bookkeeping_surfaces(deps):
    deps.update(close_started_at="2026-09-30T15:59:00+00:00", close_result=None)
    data, _, _ = await ops_launch.compute()
    assert data["close_running"] is True and data["close_result"] is None

    ops_launch.clear_cache()
    deps.update(close_result={"winners": 2, "coupon_skipped_nonflat": 1})
    data, _, _ = await ops_launch.compute()
    assert data["close_running"] is False
    assert data["close_result"] == {"winners": 2, "coupon_skipped_nonflat": 1}
