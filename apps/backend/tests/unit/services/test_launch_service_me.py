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


ONBOARDED = {"funnel_state": "complete"}
NOT_ONBOARDED = {"funnel_state": "submitted"}


def _row(**over):
    row = {
        "email": "me@x.com",
        "position": 300,
        "referral_code": "ABCD2345",
        "referrals_signed_up": 0,
        "referrals_subscribed": 0,
        "joined_at": "2026-09-10T00:00:00+00:00",
        "display_name": "Northwind Ceramics",
    }
    row.update(over)
    return row


def _patches(row, onboarding=ONBOARDED, entry=None, suggested_name=None):
    return (
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value=row)),
        patch("core.services.launch_service.launch_waitlist_repo.claim_user", AsyncMock()),
        patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=onboarding)
        ),
        patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.thelightbulb.company"),
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=entry)),
        # Stubbed at the `me()` call boundary for every test in this file —
        # `_suggested_display_name`'s own resolve/degrade logic gets its own
        # tests below. Default None matches the common (joined or
        # not-onboarded) fixtures here.
        patch("core.services.launch_service._suggested_display_name", AsyncMock(return_value=suggested_name)),
    )


async def _me(row, *, sched=None, onboarding=ONBOARDED, auth=None, entry=None, suggested_name=None):
    auth = auth or AuthContext(user_id="human_1", org_id="user_1", org_role="org:admin", email="me@x.com")
    p = _patches(row, onboarding, entry, suggested_name)
    with p[0], p[1] as claim, p[2], p[3], p[4], p[5] as entries_get, p[6] as suggested:
        body = await launch_service.me(auth, sched or _sched(+5))
    return body, claim, entries_get, suggested


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
        patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.thelightbulb.company"),
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
    ):
        body = await launch_service.me(AuthContext(user_id="user_1", org_id="org_1", email="me@x.com"), _sched(+5))
    join.assert_not_awaited()
    claim.assert_awaited_once_with("me@x.com", "user_1")
    assert body["joined"] is True and body["position"] == 300


async def test_me_shape_for_a_joined_owner():
    body, _, _, suggested = await _me(_row(referrals_subscribed=2))
    assert body == {
        "phase": "open",
        "joined": True,
        "position": 300,
        "effective_position": 280,
        "referrals_subscribed": 2,
        "referral_code": "ABCD2345",
        "referral_link": "https://dev.thelightbulb.company/?ref=ABCD2345",
        "credits_milestone_reached": 0,
        "winner": False,
        "compete_eligible": True,
        "onboarded": True,
        # The board name, projected off the SHEET row — the rename affordance
        # prefills from this, and a joined owner with no board entry (the
        # common case) has nowhere else to read it from.
        "display_name": "Northwind Ceramics",
        "suggested_display_name": None,
        "entry": None,
    }
    suggested.assert_not_awaited()  # joined already has a locked-in display_name — no Paperclip call


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
        "currency": "eur",
        "delta_cents": 5000,
        "refreshed_at": "2026-10-05T00:00:00+00:00",
        "refresh_error": None,
        "stripe_key_secret_arn": "arn:aws:secretsmanager:not-for-clients",
    }
    body, _, _, _ = await _me(_row(), entry=entry_row)
    # Exact projection — no key material, no owner_id, no baseline internals.
    assert body["entry"] == {
        "has_key": True,
        "public": False,
        "entered_at": "2026-10-02T00:00:00+00:00",
        "currency": "eur",  # the board's currency rides along — no USD default in the UI
        "delta_cents": 5000,
        "refreshed_at": "2026-10-05T00:00:00+00:00",
        "refresh_error": None,
    }


async def test_me_never_reads_the_entry_for_an_unjoined_row():
    body, _, entries_get, _ = await _me(_row(joined_at=None))
    entries_get.assert_not_awaited()
    assert body["entry"] is None


async def test_me_has_no_pot_keys():
    body, _, _, _ = await _me(_row())
    assert not any("pot" in k for k in body), body


@pytest.mark.parametrize("subs,milestone", [(0, 0), (4, 0), (5, 5), (19, 5), (20, 20), (50, 20)])
async def test_me_credits_milestone_ladder(subs, milestone):
    body, _, _, _ = await _me(_row(referrals_subscribed=subs))
    assert body["credits_milestone_reached"] == milestone


async def test_me_no_row_at_all():
    body, claim, _, suggested = await _me(None, onboarding=NOT_ONBOARDED)
    claim.assert_not_awaited()
    assert body["joined"] is False and body["position"] is None
    assert body["referral_code"] is None and body["referral_link"] is None
    assert body["winner"] is False and body["compete_eligible"] is False
    assert body["onboarded"] is False
    # Not onboarded -> nothing to prefill (join is refused anyway) -> no
    # Paperclip round trip.
    assert body["suggested_display_name"] is None
    suggested.assert_not_awaited()


async def test_me_org_row_without_joined_at_skips_suggestion():
    """A row minted by referral attribution is NOT sheet membership — only
    the explicit join stamps joined_at."""
    body, claim, _, suggested = await _me(_row(joined_at=None), suggested_name="Northwind Ceramics")
    claim.assert_awaited_once()  # the claim still runs on any existing row
    assert body["joined"] is False and body["position"] is None
    # Workspace callers cannot join the personal competition.
    assert body["suggested_display_name"] is None
    suggested.assert_not_awaited()


async def test_me_suggested_display_name_is_null_and_uncalled_once_joined():
    """The gate is on `joined`, not just on the mock's default return —
    prove Paperclip is skipped even when the (stubbed) resolver has a name
    ready to give."""
    body, _, _, suggested = await _me(_row(referrals_subscribed=2), suggested_name="Northwind Ceramics")
    assert body["joined"] is True
    assert body["suggested_display_name"] is None
    suggested.assert_not_awaited()


async def test_me_suggested_display_name_is_null_and_uncalled_for_an_org_caller():
    """POST /launch/join permanently refuses org_not_supported, so an org
    owner can never reach joined=True — an onboarded-but-unjoined org caller
    must still skip the Paperclip call (LaunchJoinBanner mounts
    unconditionally in the workspace shell, so this is every org owner's
    /launch/me read). Same proof shape as the joined-caller test above: the
    stubbed resolver has a name ready, so a non-null result would mean the
    gate is missing, not just that the mock defaulted to None."""
    org_auth = AuthContext(user_id="user_1", org_id="org_1", email="me@x.com")
    body, _, _, suggested = await _me(None, auth=org_auth, suggested_name="Northwind Ceramics")
    assert body["joined"] is False
    assert body["suggested_display_name"] is None
    suggested.assert_not_awaited()


async def test_me_post_close_winner_flags():
    row = _row(winner=True, compete_eligible=True)
    body, _, _, _ = await _me(row, sched=_sched(+5, closed_at=NOW - timedelta(hours=1)))
    assert body["phase"] == "closed"
    assert body["winner"] is True and body["compete_eligible"] is True


async def test_me_post_close_ruled_out_row():
    row = _row(winner=False, compete_eligible=False)
    body, _, _, _ = await _me(row, sched=_sched(-1))
    assert body["winner"] is False and body["compete_eligible"] is False


async def test_suggested_display_name_is_the_saved_workspace_name():
    with patch("core.services.workspace_name.saved_name", AsyncMock(return_value="Northwind Ceramics")) as saved:
        assert await launch_service._suggested_display_name("user_1") == "Northwind Ceramics"
    saved.assert_awaited_once_with("user_1")


async def test_suggested_display_name_is_none_when_no_name_was_saved():
    with patch("core.services.workspace_name.saved_name", AsyncMock(return_value=None)):
        assert await launch_service._suggested_display_name("user_1") is None


async def test_suggested_display_name_degrades_to_none_on_a_failure():
    """A lookup hiccup must never fail /launch/me."""
    with patch("core.services.workspace_name.saved_name", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await launch_service._suggested_display_name("user_1") is None
