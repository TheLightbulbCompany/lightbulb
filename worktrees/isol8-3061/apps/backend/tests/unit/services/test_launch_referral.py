"""Referral counting on referee subscription + credit milestones."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call, patch


from core.billing.metronome_funding import FundingError
from core.services import launch_phase, launch_referral

NOW = datetime.now(timezone.utc)


def _sched(closes_delta_days: int = 5, *, closed_at=None) -> launch_phase.Schedule:
    closes = NOW + timedelta(days=closes_delta_days)
    return launch_phase.Schedule(
        sheet_closes_at=closes,
        competition_start_at=closes + timedelta(hours=12),
        competition_end_at=closes + timedelta(days=61),
        comp_slots=200,
        closed_at=closed_at,
    )


REFEREE = {
    "email": "referee@x.com",
    "position": 10,
    "referral_code": "REFEREE1",
    "referred_by": "REFERRER",
    "referrals_subscribed": 0,
}
REFERRER = {
    "email": "referrer@x.com",
    "position": 5,
    "referral_code": "REFERRER",
    "user_id": "user_ref",
    "referrals_subscribed": 3,
}


_DEFAULT = object()


class _Harness:
    def __init__(self, *, sched=_DEFAULT, referee=None, referrer=None, counted_first_time=True, new_count=4):
        self.sched = _sched() if sched is _DEFAULT else sched
        self.referee = dict(REFEREE if referee is None else referee)
        self.referrer = dict(REFERRER if referrer is None else referrer)
        self.counted_first_time = counted_first_time
        self.new_count = new_count

    def __enter__(self):
        self._patches = [
            patch("core.services.launch_referral.launch_phase.schedule", return_value=self.sched),
            patch("core.services.launch_referral.user_repo.get", AsyncMock(return_value={"email": "referee@x.com"})),
            patch("core.services.launch_referral.launch_waitlist_repo.get", AsyncMock(return_value=self.referee)),
            patch(
                "core.services.launch_referral.launch_waitlist_repo.mark_referral_counted",
                AsyncMock(return_value=self.counted_first_time),
            ),
            patch(
                "core.services.launch_referral.launch_waitlist_repo.get_by_referral_code",
                AsyncMock(return_value=self.referrer),
            ),
            patch(
                "core.services.launch_referral.launch_waitlist_repo.add_counter",
                AsyncMock(return_value=self.new_count),
            ),
            patch("core.services.launch_referral.launch_waitlist_repo.update_fields", AsyncMock()),
            patch("core.billing.metronome_funding.grant_prepaid_credits", AsyncMock()),
            patch("core.services.metronome_gate.read_net_balance_microcents", AsyncMock(return_value=5_000_000)),
            patch("core.services.metronome_gate.apply_fresh_balance", AsyncMock()),
        ]
        m = [p.__enter__() for p in self._patches]
        (
            self.schedule,
            self.user_get,
            self.get,
            self.mark_counted,
            self.by_code,
            self.add_counter,
            self.update_fields,
            self.grant,
            self.read_balance,
            self.apply_balance,
        ) = m
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


async def test_counts_once_and_increments_the_referrer():
    with _Harness() as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.mark_counted.assert_awaited_once_with("referee@x.com")
    h.add_counter.assert_awaited_once_with("referrer@x.com", "referrals_subscribed", 1)


async def test_dedup_second_activation_counts_nothing():
    with _Harness(counted_first_time=False) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.add_counter.assert_not_awaited()


async def test_counting_freezes_when_the_sheet_is_not_open():
    """Jumps freeze after close — a closed_at stamp (or the deadline passing)
    stops the increment path entirely, without burning the CAS."""
    with _Harness(sched=_sched(+5, closed_at=NOW - timedelta(hours=1))) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.mark_counted.assert_not_awaited()
    h.add_counter.assert_not_awaited()


async def test_feature_off_or_org_or_no_row_is_a_noop():
    with _Harness(sched=None) as h:
        await launch_referral.on_subscription_activated("user_referee")
        h.get.assert_not_awaited()
    with _Harness() as h:
        await launch_referral.on_subscription_activated("org_acme")
        h.get.assert_not_awaited()
    with _Harness() as h:
        h.get.return_value = None
        await launch_referral.on_subscription_activated("user_referee")
        h.mark_counted.assert_not_awaited()


async def test_unattributed_referee_counts_nothing():
    referee = {k: v for k, v in REFEREE.items() if k != "referred_by"}
    with _Harness(referee=referee) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.mark_counted.assert_not_awaited()


async def test_milestone_5_grants_50_dollars_in_both_ledgers_in_order():
    calls: list[str] = []
    with _Harness(new_count=5) as h:
        h.grant.side_effect = lambda *a, **k: calls.append("grant")
        h.read_balance.side_effect = lambda *a, **k: calls.append("read") or 5_000_000
        h.apply_balance.side_effect = lambda *a, **k: calls.append("apply")
        await launch_referral.on_subscription_activated("user_referee")
    # The exact two-ledger order the checkout webhook uses: Metronome grant,
    # THEN the restore-only fast-gate refresh.
    assert calls == ["grant", "read", "apply"]
    h.grant.assert_awaited_once_with("user_ref", 5_000, idempotency_key="launch-milestone-5:user_ref")
    h.update_fields.assert_awaited_once_with("referrer@x.com", {"milestone_5_credits": "granted"})


async def test_milestone_20_tops_up_200_dollars():
    referrer = dict(REFERRER, milestone_5_credits="granted")
    with _Harness(referrer=referrer, new_count=20) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.grant.assert_awaited_once_with("user_ref", 20_000, idempotency_key="launch-milestone-20:user_ref")
    h.update_fields.assert_awaited_once_with("referrer@x.com", {"milestone_20_credits": "granted"})


async def test_crossing_20_with_5_never_granted_grants_both():
    with _Harness(new_count=20) as h:
        await launch_referral.on_subscription_activated("user_referee")
    assert h.grant.await_args_list == [
        call("user_ref", 5_000, idempotency_key="launch-milestone-5:user_ref"),
        call("user_ref", 20_000, idempotency_key="launch-milestone-20:user_ref"),
    ]


async def test_below_milestone_grants_nothing():
    with _Harness(new_count=4) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.grant.assert_not_awaited()
    h.update_fields.assert_not_awaited()


async def test_already_granted_milestone_is_not_regranted():
    referrer = dict(REFERRER, milestone_5_credits="granted")
    with _Harness(referrer=referrer, new_count=6) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.grant.assert_not_awaited()


async def test_no_metronome_ledger_stamps_pending_grant():
    with _Harness(new_count=5) as h:
        h.grant.side_effect = FundingError("no ledger")
        await launch_referral.on_subscription_activated("user_referee")
    h.update_fields.assert_awaited_once_with("referrer@x.com", {"milestone_5_credits": "pending_grant"})
    h.apply_balance.assert_not_awaited()


async def test_unclaimed_referrer_row_stamps_pending_grant_without_touching_metronome():
    referrer = {k: v for k, v in REFERRER.items() if k != "user_id"}
    with _Harness(referrer=referrer, new_count=5) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.grant.assert_not_awaited()
    h.update_fields.assert_awaited_once_with("referrer@x.com", {"milestone_5_credits": "pending_grant"})


async def test_own_activation_retries_a_pending_grant():
    """The owner activating is themselves a referrer with a pending milestone:
    their subscription is the moment a ledger appears, so retry the grant —
    even though they were referred by nobody."""
    row = {
        "email": "referee@x.com",
        "position": 10,
        "referral_code": "REFEREE1",
        "user_id": "user_referee",
        "referrals_subscribed": 7,
        "milestone_5_credits": "pending_grant",
    }
    with _Harness(referee=row) as h:
        await launch_referral.on_subscription_activated("user_referee")
    h.grant.assert_awaited_once_with("user_referee", 5_000, idempotency_key="launch-milestone-5:user_referee")
    h.update_fields.assert_awaited_once_with("referee@x.com", {"milestone_5_credits": "granted"})


async def test_an_unexpected_grant_error_stamps_nothing_so_the_next_event_retries():
    with _Harness(new_count=5) as h:
        h.grant.side_effect = RuntimeError("metronome 500")
        await launch_referral.on_subscription_activated("user_referee")  # never raises
    h.update_fields.assert_not_awaited()


async def test_never_raises_even_on_a_repo_blowup():
    with _Harness() as h:
        h.get.side_effect = RuntimeError("ddb down")
        await launch_referral.on_subscription_activated("user_referee")


async def test_email_for_owner_falls_back_to_clerk(monkeypatch):
    """A users row with no email must not silently kill referral counting.

    Rows created while the Clerk `user.created` webhook was dead carry no
    email, and this runs inside a Stripe webhook with no JWT to fall back on —
    so it asks Clerk rather than dropping the once-only count.
    """
    from core.services import clerk_admin, launch_referral

    async def _row(_owner_id):
        return {"user_id": "user_x"}  # no email

    async def _clerk(_owner_id):
        return "Them@Example.COM"

    monkeypatch.setattr(launch_referral.user_repo, "get", _row)
    monkeypatch.setattr(clerk_admin, "get_primary_email", _clerk)

    assert await launch_referral._email_for_owner("user_x") == "them@example.com"


async def test_email_for_owner_prefers_the_row_and_skips_clerk(monkeypatch):
    from core.services import clerk_admin, launch_referral

    async def _row(_owner_id):
        return {"user_id": "user_x", "email": "Row@Example.COM"}

    async def _clerk(_owner_id):
        raise AssertionError("Clerk must not be called when the row has an email")

    monkeypatch.setattr(launch_referral.user_repo, "get", _row)
    monkeypatch.setattr(clerk_admin, "get_primary_email", _clerk)

    assert await launch_referral._email_for_owner("user_x") == "row@example.com"


async def test_email_for_owner_returns_none_when_clerk_has_nothing(monkeypatch):
    from core.services import clerk_admin, launch_referral

    async def _row(_owner_id):
        return None

    async def _clerk(_owner_id):
        raise RuntimeError("clerk has no user with id user_x")

    monkeypatch.setattr(launch_referral.user_repo, "get", _row)
    monkeypatch.setattr(clerk_admin, "get_primary_email", _clerk)

    assert await launch_referral._email_for_owner("user_x") is None
