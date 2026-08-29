from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_service

NOW = datetime.now(timezone.utc)


def _sched(drop_delta_days: int) -> launch_phase.Schedule:
    drop = NOW + timedelta(days=drop_delta_days)
    return launch_phase.Schedule(drop, drop + timedelta(hours=72), drop + timedelta(hours=72, days=30), 220, 72, 30)


ACTIVATED = {"bootstrap_state": "delivered", "bootstrap_delivered_at": "2026-09-19T00:00:00+00:00"}
NOT_ACTIVATED = {"bootstrap_state": "sent", "bootstrap_delivered_at": None}


@pytest.mark.parametrize(
    "sched,billing,onboarding,expected",
    [
        (_sched(+5), {"subscription_status": "active"}, ACTIVATED, (False, "phase_waitlist")),
        (_sched(-40), {"subscription_status": "active"}, ACTIVATED, (False, "phase_ended")),
        (_sched(-1), None, ACTIVATED, (False, "no_subscription")),
        (_sched(-1), {"subscription_status": "trial_local"}, ACTIVATED, (False, "no_subscription")),
        (_sched(-1), {"subscription_status": "active"}, NOT_ACTIVATED, (False, "not_activated")),
        (_sched(-1), {"subscription_status": "active"}, ACTIVATED, (True, None)),
        (_sched(-10), {"subscription_status": "trialing"}, ACTIVATED, (True, None)),
    ],
)
def test_can_join_matrix(sched, billing, onboarding, expected):
    assert launch_service.can_join(s=sched, billing=billing, onboarding=onboarding, now=NOW) == expected


async def test_resolve_email_prefers_jwt_then_users_row():
    assert await launch_service.resolve_email(AuthContext(user_id="u1", email="J@X.com")) == "j@x.com"
    with patch(
        "core.services.launch_service.user_repo.get", AsyncMock(return_value={"user_id": "u1", "email": "row@x.com"})
    ):
        assert await launch_service.resolve_email(AuthContext(user_id="u1")) == "row@x.com"
    with patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)):
        assert await launch_service.resolve_email(AuthContext(user_id="u1")) is None


async def test_me_lazily_creates_row_claims_user_and_composes():
    auth = AuthContext(user_id="user_1", email="me@x.com")
    row = {
        "email": "me@x.com",
        "position": 300,
        "referral_code": "ABCD2345",
        "referrals_signed_up": 2,
        "referrals_paid": 1,
        "free_months_banked": 1,
    }
    with (
        patch("core.services.launch_service.launch_waitlist_repo") as wl,
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
        patch(
            "core.services.launch_service.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "active"}),
        ),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ACTIVATED)
        ),
        patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.flickmeon.ai"),
    ):
        wl.join = AsyncMock(return_value=row)
        wl.claim_user = AsyncMock()
        body = await launch_service.me(auth, _sched(-1))
    wl.join.assert_awaited_once_with("me@x.com", referred_by=None, source="signup")
    wl.claim_user.assert_awaited_once_with("me@x.com", "user_1")
    assert body["waitlist"] == {
        "position": 300,
        "referral_code": "ABCD2345",
        "in_comp_window": False,
        "referral_link": "https://dev.flickmeon.ai/?ref=ABCD2345",
        "referrals_signed_up": 2,
        "referrals_paid": 1,
        "free_months_banked": 1,
    }
    assert body["activated"] is True and body["activated_at"] == ACTIVATED["bootstrap_delivered_at"]
    assert body["entry"] is None and body["can_enter"] is True and body["reason"] is None


async def test_me_without_email_has_no_waitlist_block():
    auth = AuthContext(user_id="user_1")
    with (
        patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)),
        patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
        patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=None)),
        patch(
            "core.services.launch_service.readiness_state.resolve_onboarding_state",
            AsyncMock(return_value=NOT_ACTIVATED),
        ),
    ):
        body = await launch_service.me(auth, _sched(-1))
    assert body["waitlist"] is None and body["can_enter"] is False and body["reason"] == "no_subscription"
