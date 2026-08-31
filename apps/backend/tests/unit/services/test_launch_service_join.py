"""join_sheet — the explicit sheet join: onboarding gate, phase gate,
idempotency, and the join-confirmation email."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_service

NOW = datetime.now(timezone.utc)
AUTH = AuthContext(user_id="user_1", email="me@x.com")


def _sched(closes_delta_days: int = 5, *, closed_at=None) -> launch_phase.Schedule:
    closes = NOW + timedelta(days=closes_delta_days)
    return launch_phase.Schedule(
        sheet_closes_at=closes,
        competition_start_at=closes + timedelta(hours=12),
        competition_end_at=closes + timedelta(days=61),
        comp_slots=200,
        closed_at=closed_at,
    )


ONBOARDED = {"bootstrap_state": "delivered"}
NOT_ONBOARDED = {"bootstrap_state": "sent"}

ROW = {
    "email": "me@x.com",
    "position": 42,
    "referral_code": "ABCD2345",
    "referrals_signed_up": 0,
    "referrals_subscribed": 0,
}


class _Harness:
    def __init__(self, *, onboarding=ONBOARDED, first_join=True, row=None):
        self.onboarding = onboarding
        self.first_join = first_join
        self.row = dict(ROW if row is None else row)

    def __enter__(self):
        self._patches = [
            patch(
                "core.services.launch_service.readiness_state.resolve_onboarding_state",
                AsyncMock(return_value=self.onboarding),
            ),
            patch("core.services.launch_service.user_repo.get", AsyncMock(return_value={"email": "me@x.com"})),
            patch("core.services.launch_service.launch_waitlist_repo.join", AsyncMock(return_value=self.row)),
            patch("core.services.launch_service.launch_waitlist_repo.claim_user", AsyncMock()),
            patch(
                "core.services.launch_service.launch_waitlist_repo.mark_joined", AsyncMock(return_value=self.first_join)
            ),
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()),
            patch("core.services.launch_sheet_email.send_join_email", AsyncMock(return_value="msg_1")),
            patch("core.services.launch_service.me", AsyncMock(return_value={"joined": True})),
            patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.flickmeon.ai"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        (
            self.onb,
            self.user_get,
            self.join,
            self.claim,
            self.mark_joined,
            self.update_fields,
            self.email,
            self.me,
            _,
        ) = mocks
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


async def test_join_refuses_when_not_onboarded():
    with _Harness(onboarding=NOT_ONBOARDED) as h:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.join_sheet(AUTH, _sched(), display_name="Acme", public=True)
    assert exc.value.reason == "not_onboarded"
    h.join.assert_not_awaited()


async def test_join_refuses_when_sheet_closed_by_time():
    with _Harness() as h:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.join_sheet(AUTH, _sched(-1), display_name="Acme", public=True)
    assert exc.value.reason == "closed"
    h.join.assert_not_awaited()


async def test_join_refuses_when_closed_early_via_closed_at():
    with _Harness() as h:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.join_sheet(
                AUTH, _sched(+5, closed_at=NOW - timedelta(hours=1)), display_name="Acme", public=True
            )
    assert exc.value.reason == "closed"
    h.join.assert_not_awaited()


async def test_join_refuses_without_an_email():
    with _Harness() as h:
        h.user_get.return_value = None
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.join_sheet(AuthContext(user_id="user_1"), _sched(), display_name="A", public=True)
    assert exc.value.reason == "no_email"


async def test_first_join_writes_membership_and_sends_the_email():
    with _Harness() as h:
        body = await launch_service.join_sheet(AUTH, _sched(), display_name="  Acme  ", public=False)
    h.join.assert_awaited_once_with("me@x.com", referred_by=None, source="sheet")
    h.claim.assert_awaited_once_with("me@x.com", "user_1")
    h.mark_joined.assert_awaited_once_with("me@x.com")
    h.update_fields.assert_awaited_once_with("me@x.com", {"display_name": "Acme", "public": False})
    kwargs = h.email.await_args.kwargs
    assert kwargs["to_email"] == "me@x.com" and kwargs["position"] == 42
    assert kwargs["referral_link"] == "https://dev.flickmeon.ai/?ref=ABCD2345"
    assert kwargs["comp_slots"] == 200
    assert body == {"joined": True}  # the me() composition is the response


async def test_second_join_is_idempotent_no_email_no_overwrite():
    with _Harness(first_join=False) as h:
        await launch_service.join_sheet(AUTH, _sched(), display_name="NewName", public=True)
    h.email.assert_not_awaited()
    h.update_fields.assert_not_awaited()  # first display_name wins


async def test_join_email_uses_the_effective_position():
    row = dict(ROW, position=300, referrals_subscribed=2)
    with _Harness(row=row) as h:
        await launch_service.join_sheet(AUTH, _sched(), display_name="Acme", public=True)
    assert h.email.await_args.kwargs["position"] == 280


async def test_a_failed_email_never_fails_the_join():
    with _Harness() as h:
        h.email.side_effect = RuntimeError("ses down")
        body = await launch_service.join_sheet(AUTH, _sched(), display_name="Acme", public=True)
    assert body == {"joined": True}
