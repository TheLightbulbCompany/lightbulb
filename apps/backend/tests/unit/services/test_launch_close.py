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
            patch("core.services.launch_close.launch_phase.schedule", return_value=_sched(self.comp_slots)),
            patch(
                "core.services.launch_close.launch_phase.constants",
                return_value={
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
                AsyncMock(side_effect=lambda: [dict(r) for r in self.rows.values()]),
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
        ]
        m = [p.__enter__() for p in self._patches]
        self.mint = m[2]
        self.stripe_calls: list[tuple] = []
        return self

    async def _stripe(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", str(fn))
        self.stripe_calls.append((name, args, kwargs))
        if name == "retrieve":
            return {"id": args[0], "discount": None, "discounts": []}
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
    assert result["winners"] == 1
    # The original closed_at is preserved — force never re-stamps it.
    assert h.settings_row["closed_at"] == "2026-09-30T16:00:00+00:00"


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


async def test_close_mints_the_coupon_when_missing():
    rows = [_row("a@x.com", 1)]

    with _Harness(rows, {}, settings_row={"stripe_coupon_id": ""}) as h:

        async def _mint(auth):
            h.settings_row["stripe_coupon_id"] = "coup_minted"

        h.mint.side_effect = _mint
        await launch_close.close(ADMIN)
    h.mint.assert_awaited_once()


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
    assert len(h.sent) == 2  # emails still went out


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


async def _build_checkout_captured(discount):
    from unittest.mock import MagicMock

    from core.services import billing_service

    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return MagicMock(id="cs_1", url="https://checkout.stripe.com/x")

    with (
        patch.object(billing_service.stripe.checkout.Session, "create", fake_create),
        patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_a", "stripe_customer_id": "cus_1"}),
        ),
        patch("core.services.launch_close.winner_checkout_discount", AsyncMock(return_value=discount)),
    ):
        await billing_service._build_subscription_checkout(
            owner_id="user_a",
            line_items=[{"price": "price_flat", "quantity": 1}],
            metadata={"owner_id": "user_a"},
            success_url="https://x/ok",
            cancel_url="https://x/no",
            idempotency_key="k1",
        )
    return captured


async def test_checkout_pre_applies_the_winner_coupon_and_drops_promo_codes():
    captured = await _build_checkout_captured([{"coupon": "coup_1"}])
    assert captured["discounts"] == [{"coupon": "coup_1"}]
    # Stripe refuses discounts + allow_promotion_codes together.
    assert "allow_promotion_codes" not in captured


async def test_checkout_untouched_for_a_non_winner():
    captured = await _build_checkout_captured(None)
    assert "discounts" not in captured
    assert captured["allow_promotion_codes"] is True
