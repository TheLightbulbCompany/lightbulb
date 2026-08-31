"""The admin close action + the unpaid-winner checkout coupon pre-apply."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_close, launch_phase

NOW = datetime.now(timezone.utc)
ADMIN = AuthContext(user_id="user_admin")


def _sched(comp_slots: int = 2) -> launch_phase.Schedule:
    closes = NOW + timedelta(days=1)
    return launch_phase.Schedule(
        sheet_closes_at=closes,
        competition_start_at=closes + timedelta(hours=12),
        competition_end_at=closes + timedelta(days=61),
        comp_slots=comp_slots,
        closed_at=None,
    )


def _row(email, position, *, subs=0, user_id=None, joined=True, **over):
    row = {
        "email": email,
        "position": position,
        "referral_code": email[:8].upper(),
        "referrals_subscribed": subs,
        "user_id": user_id or f"user_{email.split('@')[0]}",
        "joined_at": "2026-09-01T00:00:00+00:00" if joined else None,
    }
    row.update(over)
    return row


class _Harness:
    """close() against an in-memory sheet. Billing rows keyed by user_id;
    update_fields mutates the in-memory rows so idempotency reruns see the
    stamps a prior run wrote."""

    def __init__(self, rows, billing, *, settings_row=None, comp_slots=2):
        self.rows = {r["email"]: r for r in rows}
        self.billing = billing
        self.settings_row = {"closed_at": None, "stripe_coupon_id": "coup_1", **(settings_row or {})}
        self.comp_slots = comp_slots
        self.close_state_writes: list[dict] = []
        self.sent: list[tuple[str, str]] = []

    async def _update_fields(self, email, fields):
        self.rows[email].update(fields)

    async def _record_close_state(self, fields):
        self.settings_row.update(fields)
        self.close_state_writes.append(dict(fields))
        return dict(self.settings_row)

    async def _effective(self):
        return dict(self.settings_row)

    def _send(self, kind, *, to_email, first_name):
        # Synchronous side_effect for the AsyncMock: the return value is what
        # the await resolves to.
        self.sent.append((kind, to_email))
        return f"msg_{to_email}"

    def __enter__(self):
        self._patches = [
            patch("core.services.launch_close.launch_settings.effective", side_effect=self._effective),
            patch(
                "core.services.launch_close.launch_settings.record_close_state", side_effect=self._record_close_state
            ),
            patch("core.services.launch_close.launch_settings.create_stripe_coupon", AsyncMock()),
            patch("core.services.launch_close.launch_phase.schedule", side_effect=lambda: _sched(self.comp_slots)),
            patch(
                "core.services.launch_close.launch_phase.constants",
                side_effect=lambda: {
                    "comp_slots": self.comp_slots,
                    "pot_front_cents": 100_000,
                    "pot_per_entrant_cents": 1_000,
                    "referral_jump": 10,
                },
            ),
            patch(
                "core.services.launch_close.launch_phase.coupon_id",
                side_effect=lambda: self.settings_row["stripe_coupon_id"],
            ),
            patch(
                "core.services.launch_close.launch_waitlist_repo.list_all",
                AsyncMock(side_effect=lambda **kw: [dict(r) for r in self.rows.values()]),
            ),
            patch("core.services.launch_close.launch_waitlist_repo.update_fields", side_effect=self._update_fields),
            patch(
                "core.services.launch_close.billing_repo.get_by_owner_id",
                AsyncMock(side_effect=lambda uid: self.billing.get(uid)),
            ),
            patch(
                "core.services.launch_close.send_winner_paid_email",
                side_effect=lambda **kw: self._send("paid", **kw),
            ),
            patch(
                "core.services.launch_close.send_winner_unpaid_email",
                side_effect=lambda **kw: self._send("unpaid", **kw),
            ),
            patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=self._stripe)),
            patch("core.services.launch_close.settings.STRIPE_FLAT_PRICE_ID", "price_flat"),
        ]
        m = [p.__enter__() for p in self._patches]
        self.mint = m[2]
        self.list_all_mock = m[6]
        self.stripe_calls: list[tuple] = []
        return self

    async def _stripe(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", str(fn))
        self.stripe_calls.append((name, args, kwargs))
        if name == "retrieve":
            if str(args[0]).startswith("coup"):
                # Coupon.retrieve — restricted by default; the stale-coupon
                # test overrides this.
                return {"id": args[0], "applies_to": {"products": ["prod_flat"]}}
            if str(args[0]).startswith("price"):
                # Price.retrieve — the flat price's product.
                return {"product": "prod_flat"}
            # Subscription.retrieve — on the flat plan by default; the
            # nonflat-winner test overrides this.
            return {
                "id": args[0],
                "discount": None,
                "discounts": [],
                "items": {"data": [{"price": {"id": "price_flat"}}]},
            }
        return SimpleNamespace(id="sub_modified")

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


PAYING = {"subscription_status": "active", "stripe_subscription_id": "sub_1"}
TRIAL = {"subscription_status": "trial_local"}


async def test_close_full_sequence():
    # 4 joined rows + 1 attribution-only row. comp_slots=2.
    # a: position 30 but 2 counted referrals -> effective 10 => winner
    # b: position 5 -> winner (paying)
    # c: position 20, paying, not a winner -> compete_eligible via payment
    # d: position 40, not paying -> ruled out
    rows = [
        _row("a@x.com", 30, subs=2),
        _row("b@x.com", 5),
        _row("c@x.com", 20),
        _row("d@x.com", 40),
        _row("e@x.com", 1, joined=False),  # never joined the sheet — invisible to close
    ]
    billing = {"user_b": PAYING, "user_c": dict(PAYING, stripe_subscription_id="sub_c"), "user_d": TRIAL}
    with _Harness(rows, billing) as h:
        result = await launch_close.close(ADMIN)
        # The roster scan must see every committed join.
        assert h.list_all_mock.await_args.kwargs == {"consistent": True}

    # closed_at stamped first.
    assert h.close_state_writes[0].keys() == {"closed_at"}
    # winners: a (eff 10) and b (5) — ordered by effective position.
    assert h.rows["a@x.com"]["winner"] is True and h.rows["b@x.com"]["winner"] is True
    assert "winner" not in h.rows["c@x.com"] and "winner" not in h.rows["d@x.com"]
    assert "winner" not in h.rows["e@x.com"]
    # eligibility: winners + paying in; unpaid non-winner out.
    assert h.rows["a@x.com"]["compete_eligible"] is True
    assert h.rows["c@x.com"]["compete_eligible"] is True
    assert h.rows["d@x.com"]["compete_eligible"] is False
    # pot frozen: roster = {a, b, c} -> 100_000 + 3 × 1_000.
    assert h.settings_row["final_roster_count"] == 3
    assert h.settings_row["final_pot_cents"] == 103_000
    # coupon attached to the PAYING winner (b) only, stable idempotency key.
    modifies = [c for c in h.stripe_calls if c[0] == "modify"]
    assert len(modifies) == 1
    assert modifies[0][1][0] == "sub_1"
    assert modifies[0][2]["discounts"] == [{"coupon": "coup_1"}]
    assert modifies[0][2]["idempotency_key"] == "launch-close-coupon:sub_1"
    assert h.rows["b@x.com"]["coupon_attached_at"]
    assert "coupon_attached_at" not in h.rows["a@x.com"]
    # emails: both winners, right variants; non-winners get nothing.
    assert sorted(h.sent) == [("paid", "b@x.com"), ("unpaid", "a@x.com")]
    assert h.rows["a@x.com"]["winner_email_sent_at"] and h.rows["b@x.com"]["winner_email_sent_at"]
    h.mint.assert_not_awaited()  # coupon already existed
    assert result["winners"] == 2 and result["final_pot_cents"] == 103_000


async def test_close_refuses_when_already_closed_unless_forced():
    rows = [_row("a@x.com", 1)]
    with _Harness(rows, {}, settings_row={"closed_at": "2026-09-30T16:00:00+00:00"}) as h:
        with pytest.raises(launch_close.AlreadyClosed):
            await launch_close.close(ADMIN)
        result = await launch_close.close(ADMIN, force=True)
    # closed_at was stamped but final_pot_cents never landed — the previous
    # close crashed mid-sequence. A force rerun is the RECOVERY path: it
    # completes winner flagging and the pot freeze (deterministic, since
    # closed_at froze joins and referral counting at stamp time).
    assert result["winners"] == 1
    assert h.rows["a@x.com"]["winner"] is True
    assert h.settings_row["final_pot_cents"] is not None
    # The original closed_at is preserved — force never re-stamps it.
    assert h.settings_row["closed_at"] == "2026-09-30T16:00:00+00:00"


async def test_force_rerun_after_a_complete_close_is_convergence_only():
    rows = [_row("a@x.com", 1)]
    with _Harness(
        rows,
        {},
        settings_row={
            "closed_at": "2026-09-30T16:00:00+00:00",
            "final_pot_cents": 100000,
            "final_roster_count": 0,
        },
    ) as h:
        result = await launch_close.close(ADMIN, force=True)
    # The sequence completed once (final_pot_cents set): winners and the pot
    # are immutable — no new flags, no pot rewrite.
    assert result["winners"] == 0
    assert "winner" not in h.rows["a@x.com"]
    assert h.settings_row["final_pot_cents"] == 100000


async def test_close_rerun_converges_and_skips_done_work():
    rows = [_row("a@x.com", 1), _row("b@x.com", 2)]
    billing = {"user_a": PAYING}
    with _Harness(rows, billing) as h:
        await launch_close.close(ADMIN)
        first_stripe = len([c for c in h.stripe_calls if c[0] == "modify"])
        first_emails = len(h.sent)
        await launch_close.close(ADMIN, force=True)
        assert len([c for c in h.stripe_calls if c[0] == "modify"]) == first_stripe  # no re-attach
        assert len(h.sent) == first_emails  # no re-send


async def test_close_rerun_retries_a_failed_email():
    rows = [_row("a@x.com", 1)]
    with _Harness(rows, {}) as h:
        # First run: the send "fails" (returns None) -> no stamp.
        with patch("core.services.launch_close.send_winner_unpaid_email", AsyncMock(return_value=None)):
            await launch_close.close(ADMIN)
        assert "winner_email_sent_at" not in h.rows["a@x.com"]
        await launch_close.close(ADMIN, force=True)
    assert h.rows["a@x.com"]["winner_email_sent_at"]
    assert h.sent == [("unpaid", "a@x.com")]


async def test_force_rerun_never_reruns_winner_flagging_or_the_pot_freeze():
    """Ruling: once closed, force = convergence ONLY. A comp_slots edit (or a
    referral landing late) between close and rerun must not grow the winner
    set, and the frozen pot keeps its first-write values."""
    rows = [_row("a@x.com", 1), _row("b@x.com", 2), _row("c@x.com", 3)]
    with _Harness(rows, {}, comp_slots=1) as h:
        await launch_close.close(ADMIN)
        assert h.rows["a@x.com"]["winner"] is True and "winner" not in h.rows["b@x.com"]
        first_pot = h.settings_row["final_pot_cents"]
        first_roster = h.settings_row["final_roster_count"]
        pot_writes = len([w for w in h.close_state_writes if "final_pot_cents" in w])

        h.comp_slots = 3  # admin edits slots after the close
        result = await launch_close.close(ADMIN, force=True)

        assert result["winners"] == 1  # the set never grows
        assert "winner" not in h.rows["b@x.com"] and "winner" not in h.rows["c@x.com"]
        assert h.settings_row["final_pot_cents"] == first_pot
        assert h.settings_row["final_roster_count"] == first_roster
        # (d) ran exactly once — the rerun wrote no pot state at all.
        assert len([w for w in h.close_state_writes if "final_pot_cents" in w]) == pot_writes == 1
        # ...and eligibility was not recomputed either (c is first-close only).
        assert "compete_eligible" not in h.rows["c@x.com"] or h.rows["c@x.com"]["compete_eligible"] is False


async def test_close_mints_the_coupon_when_missing():
    rows = [_row("a@x.com", 1)]

    with _Harness(rows, {}, settings_row={"stripe_coupon_id": ""}) as h:

        async def _mint(auth, force=False):
            assert force is False  # nothing to replace — a plain first mint
            h.settings_row["stripe_coupon_id"] = "coup_minted"

        h.mint.side_effect = _mint
        await launch_close.close(ADMIN)
    h.mint.assert_awaited_once()


async def test_close_re_mints_a_stale_unrestricted_coupon():
    """A v1 coupon minted without applies_to survives on the settings row —
    attaching it would zero bundled credits, so close() force-mints a fresh
    restricted coupon and attaches THAT."""
    rows = [_row("a@x.com", 1)]
    billing = {"user_a": PAYING}

    with _Harness(rows, billing) as h:
        real = h._stripe

        async def _unrestricted(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "retrieve" and str(args[0]) == "coup_1":
                return {"id": "coup_1"}  # no applies_to — stale v1 mint
            return await real(fn, *args, **kwargs)

        async def _mint(auth, force=False):
            assert force is True  # a deliberate replacement, never an accident
            h.settings_row["stripe_coupon_id"] = "coup_v2"

        h.mint.side_effect = _mint
        with patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=_unrestricted)):
            await launch_close.close(ADMIN)
    h.mint.assert_awaited_once()
    modifies = [c for c in h.stripe_calls if c[0] == "modify"]
    assert modifies and modifies[0][2]["discounts"] == [{"coupon": "coup_v2"}]


async def test_close_coupon_verification_failure_is_a_mint_error():
    import stripe as stripe_mod

    from core.services import launch_settings

    rows = [_row("a@x.com", 1)]
    with _Harness(rows, {}) as h:
        real = h._stripe

        async def _flaky(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "retrieve" and str(args[0]).startswith("coup"):
                raise stripe_mod.StripeError("stripe down")
            return await real(fn, *args, **kwargs)

        with patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=_flaky)):
            with pytest.raises(launch_settings.StripeMintError):
                await launch_close.close(ADMIN)


async def test_a_paying_winner_with_no_subscription_id_counts_as_a_coupon_failure():
    rows = [_row("a@x.com", 1)]
    billing = {"user_a": {"subscription_status": "active"}}  # paying shape, no sub id
    with _Harness(rows, billing) as h:
        result = await launch_close.close(ADMIN)
    assert result["coupon_failures"] == 1 and result["coupons_attached"] == 0
    assert "coupon_attached_at" not in h.rows["a@x.com"]  # unstamped -> forced rerun retries


async def test_a_paying_ultra_winner_is_skipped_and_gets_the_unpaid_email():
    """A product-restricted coupon on a non-flat sub discounts $0 without
    erroring — so an Ultra paying winner must NOT be stamped, counted as
    attached, or told "next invoice is $0". Skipped visibly; the unpaid
    variant ("first month of Starter is free at checkout") is literally
    accurate for them."""
    rows = [_row("a@x.com", 1)]
    billing = {"user_a": dict(PAYING, stripe_subscription_id="sub_ultra")}

    with _Harness(rows, billing) as h:
        real = h._stripe

        async def _ultra_sub(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "retrieve" and args and args[0] == "sub_ultra":
                return {
                    "id": "sub_ultra",
                    "discount": None,
                    "discounts": [],
                    "items": {"data": [{"price": {"id": "price_ultra"}}]},
                }
            return await real(fn, *args, **kwargs)

        with patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=_ultra_sub)):
            result = await launch_close.close(ADMIN)

    assert result["coupon_skipped_nonflat"] == 1
    assert result["coupons_attached"] == 0 and result["coupon_failures"] == 0
    assert "coupon_attached_at" not in h.rows["a@x.com"]  # no stamp
    assert [c for c in h.stripe_calls if c[0] == "modify"] == []  # no attach
    assert h.sent == [("unpaid", "a@x.com")]  # NOT the "next invoice $0" lie


async def test_mark_winner_coupon_consumed_stamps_the_sheet_row():
    update = AsyncMock()
    with (
        patch("core.services.launch_close.user_repo.get", AsyncMock(return_value={"email": "A@x.com"})),
        patch("core.services.launch_close.launch_waitlist_repo.update_fields", update),
    ):
        await launch_close.mark_winner_coupon_consumed("user_a")
    email, fields = update.await_args.args
    assert email == "a@x.com" and "coupon_attached_at" in fields


async def test_mark_winner_coupon_consumed_never_raises():
    with patch("core.services.launch_close.user_repo.get", AsyncMock(side_effect=RuntimeError("ddb down"))):
        await launch_close.mark_winner_coupon_consumed("user_a")


async def test_close_ties_break_by_raw_position():
    # Same effective position (both floor at 1) — the earlier signup wins the slot.
    rows = [_row("late@x.com", 9, subs=1), _row("early@x.com", 8, subs=1), _row("c@x.com", 50)]
    with _Harness(rows, {}, comp_slots=1) as h:
        await launch_close.close(ADMIN)
    assert h.rows["early@x.com"].get("winner") is True
    assert "winner" not in h.rows["late@x.com"]


async def test_close_requires_a_configured_schedule():
    with _Harness([], {}):
        with patch("core.services.launch_close.launch_phase.schedule", return_value=None):
            with pytest.raises(launch_close.CloseError):
                await launch_close.close(ADMIN)


async def test_a_coupon_attach_failure_does_not_abort_the_close():
    rows = [_row("a@x.com", 1), _row("b@x.com", 2)]
    billing = {"user_a": PAYING, "user_b": dict(PAYING, stripe_subscription_id="sub_b")}

    with _Harness(rows, billing) as h:
        real = h._stripe

        async def _flaky(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "modify" and args and args[0] == "sub_1":
                raise RuntimeError("stripe 500")
            return await real(fn, *args, **kwargs)

        with patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=_flaky)):
            result = await launch_close.close(ADMIN)
    assert result["coupon_failures"] == 1 and result["coupons_attached"] == 1
    assert "coupon_attached_at" not in h.rows["a@x.com"]  # not stamped -> a rerun retries
    assert h.rows["b@x.com"]["coupon_attached_at"]
    # The failed-attach winner gets NO email this run (neither variant is true
    # for them yet); the successful one is emailed normally. The failed row is
    # unstamped on both counts, so a force rerun sends the right variant once
    # the attach problem is fixed.
    assert h.sent == [("paid", "b@x.com")]
    assert "winner_email_sent_at" not in h.rows["a@x.com"]


# ---------------------------------------------------------------------------
# start_close / _run_close — the async close
# ---------------------------------------------------------------------------


def _start_patches(row):
    from unittest.mock import MagicMock

    tasks: list = []

    def _capture(coro):
        tasks.append(coro)
        return MagicMock()

    return (
        patch("core.services.launch_close.launch_settings.effective", AsyncMock(return_value=row)),
        patch("core.services.launch_close.launch_settings.record_close_state", AsyncMock()),
        patch("core.services.launch_close.launch_phase.schedule", return_value=_sched()),
        patch("core.services.launch_close.asyncio.create_task", side_effect=_capture),
        tasks,
    )


async def test_start_close_stamps_and_backgrounds_the_run():
    row = {"closed_at": None, "close_started_at": None, "close_result": None, "final_pot_cents": None}
    p0, p1, p2, p3, tasks = _start_patches(row)
    with p0, p1 as record, p2, p3:
        await launch_close.start_close(ADMIN)
    stamped = record.await_args.args[0]
    assert stamped["close_started_at"] and stamped["close_result"] is None
    assert len(tasks) == 1
    tasks[0].close()  # never awaited in this test — just captured


async def test_start_close_refuses_a_second_start_without_force():
    # closed_at already stamped (the running task got that far) — the
    # already-closed guard fires first; either way, no second task starts.
    row = {"closed_at": "t", "close_started_at": "t", "close_result": None, "final_pot_cents": None}
    p0, p1, p2, p3, tasks = _start_patches(row)
    with p0, p1 as record, p2, p3:
        with pytest.raises(launch_close.AlreadyClosed):
            await launch_close.start_close(ADMIN)
    assert tasks == []
    record.assert_not_awaited()


async def test_start_close_running_without_closed_at_is_close_running():
    # started (stamp landed) but close() crashed before stamping closed_at.
    row = {"closed_at": None, "close_started_at": "t", "close_result": None, "final_pot_cents": None}
    p0, p1, p2, p3, tasks = _start_patches(row)
    with p0, p1, p2, p3:
        with pytest.raises(launch_close.CloseRunning):
            await launch_close.start_close(ADMIN)
    assert tasks == []


async def test_start_close_force_gets_past_both_refusals():
    row = {"closed_at": "t", "close_started_at": "t", "close_result": None, "final_pot_cents": 1}
    p0, p1, p2, p3, tasks = _start_patches(row)
    with p0, p1, p2, p3:
        await launch_close.start_close(ADMIN, force=True)
    assert len(tasks) == 1
    tasks[0].close()


async def test_run_close_records_the_summary_on_the_settings_row():
    summary = {"winners": 2, "coupons_attached": 1}
    with (
        patch("core.services.launch_close.close", AsyncMock(return_value=summary)),
        patch("core.services.launch_close.launch_settings.record_close_state", AsyncMock()) as record,
    ):
        await launch_close._run_close(ADMIN, force=False)
    record.assert_awaited_once_with({"close_result": summary})


async def test_run_close_records_an_error_result_instead_of_vanishing():
    with (
        patch("core.services.launch_close.close", AsyncMock(side_effect=RuntimeError("boom"))),
        patch("core.services.launch_close.launch_settings.record_close_state", AsyncMock()) as record,
    ):
        await launch_close._run_close(ADMIN, force=False)
    record.assert_awaited_once_with({"close_result": {"error": "close_failed"}})


async def _attach(retrieved_sub):
    calls = []

    async def _stripe(fn, *args, **kwargs):
        calls.append((getattr(fn, "__name__", ""), args, kwargs))
        if getattr(fn, "__name__", "") == "retrieve":
            return retrieved_sub
        return {}

    with (
        patch("core.services.launch_close.run_in_thread", AsyncMock(side_effect=_stripe)),
        patch("core.services.launch_close.settings.STRIPE_FLAT_PRICE_ID", "price_flat"),
    ):
        done = await launch_close._attach_coupon({"stripe_subscription_id": "sub_1"}, "coup_1")
    return done, [c for c in calls if c[0] == "modify"]


_FLAT_ITEMS = {"data": [{"price": {"id": "price_flat"}}]}


async def test_attach_skips_when_our_coupon_is_already_on_the_sub():
    done, modifies = await _attach({"items": _FLAT_ITEMS, "discounts": [{"id": "di_1", "coupon": {"id": "coup_1"}}]})
    assert done == "attached" and modifies == []


async def test_attach_stacks_alongside_an_unrelated_discount():
    """A legacy free-month coupon on the sub must NOT eat the winner's month —
    ours is added alongside it, not skipped."""
    done, modifies = await _attach(
        {"items": _FLAT_ITEMS, "discounts": [{"id": "di_legacy", "coupon": {"id": "coup_old"}}]}
    )
    assert done == "attached" and len(modifies) == 1
    assert modifies[0][2]["discounts"] == [{"discount": "di_legacy"}, {"coupon": "coup_1"}]


async def test_attach_handles_the_legacy_single_discount_shape():
    done, modifies = await _attach(
        {"items": _FLAT_ITEMS, "discounts": [], "discount": {"id": "di_old", "coupon": {"id": "coup_old"}}}
    )
    assert done == "attached"
    assert modifies[0][2]["discounts"] == [{"discount": "di_old"}, {"coupon": "coup_1"}]


# ---------------------------------------------------------------------------
# winner_checkout_discount — the unpaid-winner checkout pre-apply
# ---------------------------------------------------------------------------


def _discount_patches(row, coupon="coup_1"):
    return (
        patch("core.services.launch_close.launch_phase.schedule", return_value=_sched()),
        patch("core.services.launch_close.launch_phase.coupon_id", return_value=coupon),
        patch("core.services.launch_close.user_repo.get", AsyncMock(return_value={"email": "a@x.com"})),
        patch("core.services.launch_close.launch_waitlist_repo.get", AsyncMock(return_value=row)),
    )


async def test_winner_discount_applies_for_an_unattached_winner():
    p = _discount_patches(_row("a@x.com", 1, winner=True))
    with p[0], p[1], p[2], p[3]:
        assert await launch_close.winner_checkout_discount("user_a") == [{"coupon": "coup_1"}]


@pytest.mark.parametrize(
    "row",
    [
        _row("a@x.com", 1),  # not a winner
        _row("a@x.com", 1, winner=True, coupon_attached_at="2026-09-30T16:00:00+00:00"),  # already consumed
        None,  # no sheet row
    ],
)
async def test_winner_discount_absent_otherwise(row):
    p = _discount_patches(row)
    with p[0], p[1], p[2], p[3]:
        assert await launch_close.winner_checkout_discount("user_a") is None


async def test_winner_discount_absent_once_the_coupon_redeem_by_has_passed():
    """A flagged winner subscribing after Oct 31 gets a NORMAL full-price
    checkout — an expired coupon on Session.create is an uncaught Stripe
    InvalidRequestError (500), not a discount."""
    from datetime import timedelta

    p = _discount_patches(_row("a@x.com", 1, winner=True))
    with p[0], p[1], p[2], p[3]:
        with patch(
            "core.services.launch_settings.COUPON_REDEEM_BY",
            datetime.now(timezone.utc) - timedelta(days=1),
        ):
            assert await launch_close.winner_checkout_discount("user_a") is None


async def test_winner_discount_never_raises():
    p = _discount_patches(_row("a@x.com", 1, winner=True))
    with p[0], p[1], p[2]:
        with patch(
            "core.services.launch_close.launch_waitlist_repo.get", AsyncMock(side_effect=RuntimeError("ddb down"))
        ):
            assert await launch_close.winner_checkout_discount("user_a") is None


# ---------------------------------------------------------------------------
# Checkout injection — _build_subscription_checkout applies the discount
# ---------------------------------------------------------------------------


async def _build_checkout_captured(discount, *, price="price_flat", line_items=None):
    from unittest.mock import MagicMock

    from core.services import billing_service

    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return MagicMock(id="cs_1", url="https://checkout.stripe.com/x")

    lookup = AsyncMock(return_value=discount)
    with (
        patch.object(billing_service.stripe.checkout.Session, "create", fake_create),
        patch.object(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat"),
        patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_a", "stripe_customer_id": "cus_1"}),
        ),
        patch("core.services.launch_close.winner_checkout_discount", lookup),
    ):
        await billing_service._build_subscription_checkout(
            owner_id="user_a",
            line_items=line_items if line_items is not None else [{"price": price, "quantity": 1}],
            metadata={"owner_id": "user_a"},
            success_url="https://x/ok",
            cancel_url="https://x/no",
            idempotency_key="k1",
        )
    return captured, lookup


async def test_checkout_pre_applies_the_winner_coupon_and_drops_promo_codes():
    captured, _ = await _build_checkout_captured([{"coupon": "coup_1"}])
    assert captured["discounts"] == [{"coupon": "coup_1"}]
    # Stripe refuses discounts + allow_promotion_codes together.
    assert "allow_promotion_codes" not in captured
    # The consumed-marker rides BOTH metadata targets so the checkout webhook
    # can stamp the sheet row (no second free month on a forced rerun).
    assert captured["metadata"]["launch_winner_coupon"] == "true"
    assert captured["subscription_data"]["metadata"]["launch_winner_coupon"] == "true"


async def test_checkout_untouched_for_a_non_winner():
    captured, _ = await _build_checkout_captured(None)
    assert "discounts" not in captured
    assert captured["allow_promotion_codes"] is True
    assert "launch_winner_coupon" not in captured["metadata"]


async def test_bundled_credits_checkout_keeps_the_discount_and_the_credits_line():
    """The BUNDLED shape end to end: a flagged winner picking starter credits
    gets a session with the flat line AND the one-time credits line, the
    coupon pre-applied, and the credits line passed through untouched — the
    coupon's applies_to product restriction (minted in launch_settings) is
    what keeps the discount off the credits line at Stripe, so the credits
    are always paid while the Starter month is free."""
    credits_line = {
        "price_data": {
            "currency": "usd",
            "unit_amount": 10_000,
            "product_data": {"name": "Claude credits", "description": "Prepaid Claude inference balance."},
        },
        "quantity": 1,
    }
    items = [{"price": "price_flat", "quantity": 1}, credits_line]
    captured, _ = await _build_checkout_captured([{"coupon": "coup_1"}], line_items=items)
    assert captured["discounts"] == [{"coupon": "coup_1"}]
    assert "allow_promotion_codes" not in captured
    assert captured["metadata"]["launch_winner_coupon"] == "true"
    assert captured["subscription_data"]["metadata"]["launch_winner_coupon"] == "true"
    assert captured["line_items"] == items  # credits line verbatim — never discounted app-side


async def test_ultra_checkout_gets_no_discount_even_for_a_winner():
    """The coupon is product-restricted to Starter — an Ultra session gets no
    discount at all (the lookup is never even consulted), rather than a
    Stripe applicability error. Free month = Starter month."""
    captured, lookup = await _build_checkout_captured([{"coupon": "coup_1"}], price="price_ultra")
    assert "discounts" not in captured
    assert captured["allow_promotion_codes"] is True
    lookup.assert_not_awaited()
