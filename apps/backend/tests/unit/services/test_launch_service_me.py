from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_service

NOW = datetime.now(timezone.utc)


def _sched(closes_delta_days: int, *, comp_slots: int = 200, closed_at=None) -> launch_phase.Schedule:
    closes = NOW + timedelta(days=closes_delta_days)
    return launch_phase.Schedule(
        sheet_closes_at=closes,
        competition_start_at=closes + timedelta(hours=12),
        competition_end_at=closes + timedelta(days=61),
        comp_slots=comp_slots,
        closed_at=closed_at,
    )


ONBOARDED = {"bootstrap_state": "delivered", "bootstrap_delivered_at": "2026-09-19T00:00:00+00:00"}
NOT_ONBOARDED = {"bootstrap_state": "sent", "bootstrap_delivered_at": None}


def _row(**over):
    row = {
        "email": "me@x.com",
        "position": 300,
        "referral_code": "ABCD2345",
        "referrals_signed_up": 0,
        "referrals_subscribed": 0,
        "joined_at": "2026-09-10T00:00:00+00:00",
    }
    row.update(over)
    return row


def _patches(row, onboarding=ONBOARDED, entry=None):
    return (
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value=row)),
        patch("core.services.launch_service.launch_waitlist_repo.claim_user", AsyncMock()),
        patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=onboarding)
        ),
        patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.flickmeon.ai"),
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=entry)),
    )


async def _me(row, *, sched=None, onboarding=ONBOARDED, auth=None, entry=None):
    auth = auth or AuthContext(user_id="user_1", email="me@x.com")
    p = _patches(row, onboarding, entry)
    with p[0], p[1] as claim, p[2], p[3], p[4], p[5] as entries_get:
        body = await launch_service.me(auth, sched or _sched(+5))
    return body, claim, entries_get


def test_effective_position_uses_subscribed_referrals_with_the_20_cap():
    jump = 10
    assert launch_service.effective_position(_row(referrals_subscribed=0), jump) == 300
    assert launch_service.effective_position(_row(referrals_subscribed=2), jump) == 280
    # Capped at 20 counted — 50 referrals move no further than 20 do.
    assert launch_service.effective_position(_row(referrals_subscribed=20), jump) == 100
    assert launch_service.effective_position(_row(referrals_subscribed=50), jump) == 100
    # Floored at 1.
    assert launch_service.effective_position(_row(position=5, referrals_subscribed=3), jump) == 1
    # Signed-up-but-not-subscribed referrals move nothing.
    assert launch_service.effective_position(_row(referrals_signed_up=9), jump) == 300


async def test_resolve_email_prefers_users_row_then_jwt():
    with patch(
        "core.services.launch_service.user_repo.get", AsyncMock(return_value={"user_id": "u1", "email": "Row@X.com"})
    ):
        assert await launch_service.resolve_email(AuthContext(user_id="u1", email="jwt@x.com")) == "row@x.com"
    with patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)):
        assert await launch_service.resolve_email(AuthContext(user_id="u1", email="J@X.com")) == "j@x.com"
        assert await launch_service.resolve_email(AuthContext(user_id="u1")) is None


async def test_me_is_read_only_and_claims_the_row():
    """No join-on-read — me() never creates a row; the one write is the
    claim_user stamp on an existing row."""
    with (
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value=_row())),
        patch("core.services.launch_service.launch_waitlist_repo.claim_user", AsyncMock()) as claim,
        patch("core.services.launch_service.launch_waitlist_repo.join", AsyncMock()) as join,
        patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ONBOARDED)
        ),
        patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.flickmeon.ai"),
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
    ):
        body = await launch_service.me(AuthContext(user_id="user_1", email="me@x.com"), _sched(+5))
    join.assert_not_awaited()
    claim.assert_awaited_once_with("me@x.com", "user_1")
    assert body["joined"] is True and body["position"] == 300


async def test_me_shape_for_a_joined_owner():
    body, _, _ = await _me(_row(referrals_subscribed=2))
    assert body == {
        "phase": "open",
        "joined": True,
        "position": 300,
        "effective_position": 280,
        "referrals_subscribed": 2,
        "referral_code": "ABCD2345",
        "referral_link": "https://dev.flickmeon.ai/?ref=ABCD2345",
        "credits_milestone_reached": 0,
        "winner": False,
        "compete_eligible": True,
        "onboarded": True,
        "entry": None,
    }


async def test_me_carries_the_board_entry_for_a_joined_owner():
    """Reloading /launch mid-competition must show the connected board state
    instead of inviting a key re-paste — the entry projection is the only
    read surface for it."""
    entry_row = {
        "owner_id": "user_1",
        "display_name": "Ada",
        "has_key": True,
        "public": False,
        "entered_at": "2026-10-02T00:00:00+00:00",
        "delta_cents": 5000,
        "refreshed_at": "2026-10-05T00:00:00+00:00",
        "refresh_error": None,
        "stripe_key_secret_arn": "arn:aws:secretsmanager:not-for-clients",
    }
    body, _, _ = await _me(_row(), entry=entry_row)
    # Exact projection — no key material, no owner_id, no baseline internals.
    assert body["entry"] == {
        "has_key": True,
        "public": False,
        "entered_at": "2026-10-02T00:00:00+00:00",
        "delta_cents": 5000,
        "refreshed_at": "2026-10-05T00:00:00+00:00",
        "refresh_error": None,
    }


async def test_me_never_reads_the_entry_for_an_unjoined_row():
    body, _, entries_get = await _me(_row(joined_at=None))
    entries_get.assert_not_awaited()
    assert body["entry"] is None


async def test_me_has_no_pot_keys():
    body, _, _ = await _me(_row())
    assert not any("pot" in k for k in body), body


@pytest.mark.parametrize("subs,milestone", [(0, 0), (4, 0), (5, 5), (19, 5), (20, 20), (50, 20)])
async def test_me_credits_milestone_ladder(subs, milestone):
    body, _, _ = await _me(_row(referrals_subscribed=subs))
    assert body["credits_milestone_reached"] == milestone


async def test_me_no_row_at_all():
    body, claim, _ = await _me(None, onboarding=NOT_ONBOARDED)
    claim.assert_not_awaited()
    assert body["joined"] is False and body["position"] is None
    assert body["referral_code"] is None and body["referral_link"] is None
    assert body["winner"] is False and body["compete_eligible"] is False
    assert body["onboarded"] is False


async def test_me_row_without_joined_at_is_not_joined():
    """A row minted by referral attribution is NOT sheet membership — only
    the explicit join stamps joined_at."""
    body, claim, _ = await _me(_row(joined_at=None))
    claim.assert_awaited_once()  # the claim still runs on any existing row
    assert body["joined"] is False and body["position"] is None


async def test_me_post_close_winner_flags():
    row = _row(winner=True, compete_eligible=True)
    body, _, _ = await _me(row, sched=_sched(+5, closed_at=NOW - timedelta(hours=1)))
    assert body["phase"] == "closed"
    assert body["winner"] is True and body["compete_eligible"] is True


async def test_me_post_close_ruled_out_row():
    row = _row(winner=False, compete_eligible=False)
    body, _, _ = await _me(row, sched=_sched(-1))
    assert body["winner"] is False and body["compete_eligible"] is False
