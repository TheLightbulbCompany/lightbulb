"""join_sheet — the explicit sheet join: phase gate, idempotency, and the
join-confirmation email. There is deliberately NO onboarding gate; the first
two tests pin that, because the competition sign-up modal asks while the
owner's container is still provisioning."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_service

NOW = datetime.now(timezone.utc)
AUTH = AuthContext(user_id="human_1", org_id="user_1", org_role="org:admin", email="me@x.com")


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


async def test_join_allows_an_owner_whose_container_is_still_provisioning():
    """The competition sign-up modal asks DURING provisioning — that is the
    whole point of it — so an owner whose bootstrap has not delivered must be
    able to join. This used to raise LaunchRefused("not_onboarded")."""
    with _Harness(onboarding=NOT_ONBOARDED) as h:
        out = await launch_service.join_sheet(AUTH, _sched(), display_name="Acme", public=True)
    assert out == {"joined": True}
    h.join.assert_awaited_once()
    h.mark_joined.assert_awaited_once()


async def test_join_never_reads_onboarding_state():
    """Not merely permissive — the read is gone. It was a Paperclip round trip
    on the join path that could only ever refuse a caller."""
    with _Harness(onboarding=NOT_ONBOARDED) as h:
        await launch_service.join_sheet(AUTH, _sched(), display_name="Acme", public=True)
    h.onb.assert_not_awaited()


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
            await launch_service.join_sheet(
                AuthContext(user_id="human_1", org_id="user_1", org_role="org:admin"),
                _sched(),
                display_name="A",
                public=True,
            )
    assert exc.value.reason == "no_email"


async def test_first_join_writes_membership_and_sends_the_email():
    with _Harness() as h:
        body = await launch_service.join_sheet(AUTH, _sched(), display_name="  Acme  ", public=False)
    h.join.assert_awaited_once_with("me@x.com", referred_by=None, source="sheet")
    h.claim.assert_awaited_once_with("me@x.com", "human_1")
    # ONE atomic write carries membership + the join-time fields.
    h.mark_joined.assert_awaited_once_with("me@x.com", display_name="Acme", public=False)
    h.update_fields.assert_not_awaited()
    kwargs = h.email.await_args.kwargs
    assert kwargs["to_email"] == "me@x.com" and kwargs["position"] == 42
    assert kwargs["referral_link"] == "https://dev.flickmeon.ai/?ref=ABCD2345"
    assert kwargs["comp_slots"] == 200
    assert body == {"joined": True}  # the me() composition is the response


async def test_second_join_is_idempotent_no_email_no_overwrite():
    with _Harness(first_join=False) as h:
        await launch_service.join_sheet(AUTH, _sched(), display_name="NewName", public=True)
    h.email.assert_not_awaited()
    # First join wins wholesale — the repo CAS refused, nothing else written.
    h.update_fields.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Board gating — connect_stripe / set_public / leaderboard honor the close
# ---------------------------------------------------------------------------


def _board_patches(row):
    return (
        patch("core.services.launch_service.user_repo.get", AsyncMock(return_value={"email": "me@x.com"})),
        patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value=row)),
    )


@pytest.mark.parametrize("action", ["connect_stripe", "set_public"])
async def test_board_actions_refused_for_a_ruled_out_row(action):
    row = dict(ROW, joined_at="t", compete_eligible=False)
    p0, p1 = _board_patches(row)
    with p0, p1:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            if action == "connect_stripe":
                await launch_service.connect_stripe(AUTH, _sched(-1), stripe_key="rk_test_x")
            else:
                await launch_service.set_public(AUTH, _sched(-1), public=True)
    assert exc.value.reason == "not_eligible"


@pytest.mark.parametrize("action", ["connect_stripe", "set_public"])
async def test_board_actions_refused_once_the_competition_ended(action):
    row = dict(ROW, joined_at="t", compete_eligible=True)
    ended = _sched(-100)  # sheet closed and competition end long past
    p0, p1 = _board_patches(row)
    with p0, p1:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            if action == "connect_stripe":
                await launch_service.connect_stripe(AUTH, ended, stripe_key="rk_test_x")
            else:
                await launch_service.set_public(AUTH, ended, public=True)
    assert exc.value.reason == "ended"


async def test_pre_close_joined_rows_are_eligible_for_the_board():
    """No compete_eligible flag yet (pre-close) — the gate must treat the
    row as eligible, not refuse it."""
    row = dict(ROW, joined_at="t")  # no compete_eligible key
    p0, p1 = _board_patches(row)
    with p0, p1:
        with (
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()) as update,
            patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
        ):
            result = await launch_service.set_public(AUTH, _sched(-1), public=False)
    assert result == {"public": False}
    update.assert_awaited_once_with("me@x.com", {"public": False})


async def test_leaderboard_filters_ruled_out_rows():
    entries = [
        {"owner_id": "user_in", "display_name": "In", "has_key": True, "public": True, "delta_cents": 100},
        {"owner_id": "user_out", "display_name": "Out", "has_key": True, "public": True, "delta_cents": 900},
        {"owner_id": "user_legacy", "display_name": "Legacy", "has_key": True, "public": True, "delta_cents": 50},
    ]
    sheet = [
        {"email": "in@x.com", "user_id": "user_in", "compete_eligible": True},
        {"email": "out@x.com", "user_id": "user_out", "compete_eligible": False},
        # user_legacy has no sheet row at all — stays visible.
    ]
    with (
        patch("core.services.launch_service.launch_entries_repo.list_all", AsyncMock(return_value=entries)),
        patch("core.services.launch_service.launch_waitlist_repo.list_all", AsyncMock(return_value=sheet)),
    ):
        board = await launch_service.leaderboard()
    names = [r["display_name"] for r in board["rows"]]
    assert names == ["In", "Legacy"]  # ruled-out row gone, ranked by delta
    assert board["competing"] == 2 and board["on_board"] == 2


# --- rename_display_name -----------------------------------------------------
# The sheet's display_name is written ONCE by mark_joined
# (attribute_not_exists(joined_at)), so before this the first name an owner
# typed was permanent on a public board. That was survivable while joining
# happened after Friday had named the company; it is not, now that the modal
# asks during provisioning.


async def test_rename_writes_the_sheet_row_and_skips_a_missing_entry():
    row = dict(ROW, joined_at="t", display_name="Old")
    p0, p1 = _board_patches(row)
    with p0, p1:
        with (
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()) as update,
            patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)),
            patch("core.services.launch_service.launch_entries_repo.set_display_name", AsyncMock()) as set_name,
        ):
            out = await launch_service.rename_display_name(AUTH, _sched(), display_name="  New Co  ")
    assert out == {"display_name": "New Co"}  # stripped
    update.assert_awaited_once_with("me@x.com", {"display_name": "New Co"})
    set_name.assert_not_awaited()  # no board entry to carry it to


async def test_rename_also_updates_the_board_entry_when_one_exists():
    """connect_stripe COPIES the name onto the entry, and the leaderboard reads
    the copy — a rename that only wrote the sheet row would leave the public
    board showing the old name forever."""
    row = dict(ROW, joined_at="t", display_name="Old")
    p0, p1 = _board_patches(row)
    with p0, p1:
        with (
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()),
            patch(
                "core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value={"owner_id": "user_1"})
            ),
            patch("core.services.launch_service.launch_entries_repo.set_display_name", AsyncMock()) as set_name,
        ):
            await launch_service.rename_display_name(AUTH, _sched(), display_name="New Co")
    set_name.assert_awaited_once_with("user_1", "New Co")


async def test_rename_refused_for_a_row_that_never_joined():
    p0, p1 = _board_patches(dict(ROW))  # no joined_at
    with p0, p1:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.rename_display_name(AUTH, _sched(), display_name="New Co")
    assert exc.value.reason == "not_joined"


async def test_rename_survives_the_board_entry_vanishing_mid_write():
    """The entry can be deleted (admin takedown) between the read and the
    write. The sheet row is ALREADY renamed by then, so raising would 500 a
    half-done request and RenameForm would loop the owner on "try again"
    forever. connect_stripe handles the same race explicitly."""
    from botocore.exceptions import ClientError

    row = dict(ROW, joined_at="t", display_name="Old")
    p0, p1 = _board_patches(row)
    conditional = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    with p0, p1:
        with (
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()) as update,
            patch(
                "core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value={"owner_id": "user_1"})
            ),
            patch(
                "core.services.launch_service.launch_entries_repo.set_display_name",
                AsyncMock(side_effect=conditional),
            ),
        ):
            out = await launch_service.rename_display_name(AUTH, _sched(), display_name="New Co")
    assert out == {"display_name": "New Co"}
    update.assert_awaited_once_with("me@x.com", {"display_name": "New Co"})


async def test_rename_still_raises_a_real_dynamo_error():
    """Only the vanished-entry condition is swallowed — a throttle or an IAM
    failure must still surface rather than reporting a rename that half ran."""
    from botocore.exceptions import ClientError

    row = dict(ROW, joined_at="t", display_name="Old")
    p0, p1 = _board_patches(row)
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with p0, p1:
        with (
            patch("core.services.launch_service.launch_waitlist_repo.update_fields", AsyncMock()),
            patch(
                "core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value={"owner_id": "user_1"})
            ),
            patch("core.services.launch_service.launch_entries_repo.set_display_name", AsyncMock(side_effect=boom)),
        ):
            with pytest.raises(ClientError):
                await launch_service.rename_display_name(AUTH, _sched(), display_name="New Co")


async def test_rename_refused_once_the_competition_has_ended():
    """Same gate as the other board actions: the board is frozen at the end."""
    row = dict(ROW, joined_at="t")
    p0, p1 = _board_patches(row)
    with p0, p1:
        with pytest.raises(launch_service.LaunchRefused) as exc:
            await launch_service.rename_display_name(AUTH, _sched(-100), display_name="New Co")
    assert exc.value.reason == "ended"
