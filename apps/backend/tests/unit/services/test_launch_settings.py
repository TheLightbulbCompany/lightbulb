from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import stripe

from core.auth import AuthContext
from core.services import launch_settings as ls

GOOD_DROP = "2026-09-18T00:00:00-04:00"
GOOD_START = "2026-09-21T00:00:00-04:00"
ADMIN = AuthContext(user_id="user_admin")


def _payload(**over):
    p = {
        "enabled": True,
        "drop_at": GOOD_DROP,
        "competition_start_at": GOOD_START,
        "comp_slots": 220,
        "competition_days": 30,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "stripe_coupon_id": "coupon_1",
        "stripe_promotion_code_id": "promo_1",
        "stripe_promotion_code": "LIGHTBULB-ABCD",
    }
    p.update(over)
    return p


def test_defaults_shape():
    assert ls.DEFAULTS == {
        "enabled": False,
        "drop_at": None,
        "competition_start_at": None,
        "comp_slots": 220,
        "competition_days": 30,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "stripe_coupon_id": "",
        "stripe_promotion_code_id": "",
        "stripe_promotion_code": "",
        "mint_nonce": 0,
    }


def test_validate_accepts_a_full_valid_payload():
    row = ls.validate(_payload())
    assert row["enabled"] is True
    assert row["drop_at"] == GOOD_DROP and row["competition_start_at"] == GOOD_START
    assert row["comp_slots"] == 220 and row["competition_days"] == 30
    assert row["pot_front_cents"] == 100_000 and row["pot_per_entrant_cents"] == 1_000
    assert row["stripe_coupon_id"] == "coupon_1"


def test_validate_fills_missing_fields_from_defaults():
    # mint_nonce isn't a validate()-owned field — it's server-only, managed
    # exclusively by create_stripe_coupon() (see update()'s docstring).
    assert ls.validate({}) == {k: v for k, v in ls.DEFAULTS.items() if k != "mint_nonce"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("drop_at", "not a date at all"),
        ("drop_at", "2026/09/18 00:00:00-04:00"),  # wrong separators — bad grammar
        ("competition_start_at", "18-09-2026T00:00:00-04:00"),  # day/month swapped
    ],
)
def test_validate_rejects_bad_grammar_dates(field, value):
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(**{field: value}))
    assert field in exc.value.errors


def test_validate_rejects_a_naive_timestamp_with_no_offset():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(drop_at="2026-09-18T00:00:00"))
    assert "drop_at" in exc.value.errors


def test_validate_rejects_24_00():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(competition_start_at="2026-09-21T24:00:00-04:00"))
    assert "competition_start_at" in exc.value.errors


def test_validate_rejects_feb_29_on_a_non_leap_year():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(drop_at="2026-02-29T00:00:00-04:00"))
    assert "drop_at" in exc.value.errors


def test_validate_rejects_start_before_drop():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(drop_at=GOOD_START, competition_start_at=GOOD_DROP))
    assert "competition_start_at" in exc.value.errors


def test_validate_rejects_start_equal_to_drop():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(drop_at=GOOD_DROP, competition_start_at=GOOD_DROP))
    assert "competition_start_at" in exc.value.errors


@pytest.mark.parametrize(
    "field,value",
    [
        ("comp_slots", -1),
        ("comp_slots", 0),
        ("competition_days", 0),
        ("pot_front_cents", -1),
        ("pot_per_entrant_cents", -1),
    ],
)
def test_validate_rejects_out_of_range_ints(field, value):
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(**{field: value}))
    assert field in exc.value.errors


def test_validate_rejects_a_non_numeric_int_field():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(comp_slots="a lot"))
    assert "comp_slots" in exc.value.errors


def test_validate_allows_zero_pot_cents():
    row = ls.validate(_payload(pot_front_cents=0, pot_per_entrant_cents=0))
    assert row["pot_front_cents"] == 0 and row["pot_per_entrant_cents"] == 0


def test_validate_rejects_enabled_without_either_date():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(enabled=True, drop_at=None, competition_start_at=None))
    assert "enabled" in exc.value.errors


def test_validate_rejects_enabled_missing_one_date():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(enabled=True, competition_start_at=None))
    assert "enabled" in exc.value.errors


def test_validate_allows_disabled_with_no_dates():
    row = ls.validate(_payload(enabled=False, drop_at=None, competition_start_at=None))
    assert row["enabled"] is False and row["drop_at"] is None


def test_validate_collects_multiple_field_errors_at_once():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(comp_slots=-1, competition_days=-1, drop_at="garbage"))
    assert {"comp_slots", "competition_days", "drop_at"} <= set(exc.value.errors)


def test_to_public_drops_updated_by_but_keeps_updated_at():
    row = dict(ls.DEFAULTS, updated_at="2026-08-29T00:00:00+00:00", updated_by="user_admin")
    public = ls.to_public(row)
    assert "updated_by" not in public
    assert public["updated_at"] == "2026-08-29T00:00:00+00:00"


async def test_effective_merges_defaults_with_the_stored_row():
    with patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value={"comp_slots": 42})):
        row = await ls.effective()
    assert row["comp_slots"] == 42  # from the stored row
    assert row["competition_days"] == 30  # from DEFAULTS — not stored


async def test_effective_is_all_defaults_when_no_row_saved_yet():
    with patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=None)):
        assert await ls.effective() == ls.DEFAULTS


# ---------------------------------------------------------------------------
# update() — validate, persist, clear caches
# ---------------------------------------------------------------------------


async def test_update_persists_the_validated_row_stamped_with_the_writer():
    put_mock = AsyncMock(side_effect=lambda item: {**item, "updated_at": "2026-08-29T00:00:00+00:00"})
    with (
        patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=None)),
        patch.object(ls.launch_settings_repo, "put", put_mock),
        patch.object(ls, "_clear_caches") as clear,
    ):
        public_row = await ls.update(ADMIN, _payload())
    assert public_row["enabled"] is True
    assert "updated_by" not in public_row  # to_public strips it from the response
    assert "mint_nonce" not in public_row  # to_public strips it too — server-only
    assert public_row["updated_at"] == "2026-08-29T00:00:00+00:00"
    written = put_mock.call_args.args[0]
    assert written["updated_by"] == "user_admin"  # but the repo write DOES carry it
    clear.assert_called_once()


async def test_update_raises_settings_invalid_without_writing_or_clearing_caches():
    put_mock = AsyncMock()
    with (
        patch.object(ls.launch_settings_repo, "put", put_mock),
        patch.object(ls, "_clear_caches") as clear,
        pytest.raises(ls.SettingsInvalid) as exc,
    ):
        await ls.update(ADMIN, _payload(comp_slots=0))
    assert "comp_slots" in exc.value.errors
    put_mock.assert_not_called()
    clear.assert_not_called()


async def test_update_clears_all_three_caches():
    with (
        patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=None)),
        patch.object(ls.launch_settings_repo, "put", AsyncMock(side_effect=lambda item: item)),
        patch.object(ls.launch_phase, "invalidate") as invalidate,
        patch("routers.launch.clear_response_cache") as clear_response_cache,
        patch("core.services.ops.launch.clear_cache") as clear_ops_cache,
    ):
        await ls.update(ADMIN, _payload())
    invalidate.assert_called_once()
    clear_response_cache.assert_called_once()
    clear_ops_cache.assert_called_once()


async def test_update_preserves_coupon_managed_fields_regardless_of_payload():
    """A settings save can never blank out an already-minted coupon or reset
    the idempotency nonce — those four fields are carried forward from the
    STORED row, not trusted from the PUT body, no matter what the body
    claims for them (see fix-round note in update()'s docstring)."""
    stored = {
        **ls.DEFAULTS,
        "mint_nonce": 3,
        "stripe_coupon_id": "coupon_real",
        "stripe_promotion_code_id": "promo_real",
        "stripe_promotion_code": "LIGHTBULB-REAL01",
    }
    put_mock = AsyncMock(side_effect=lambda item: item)
    with (
        patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=stored)),
        patch.object(ls.launch_settings_repo, "put", put_mock),
        patch.object(ls, "_clear_caches"),
    ):
        # The body claims different (garbage) values for all four managed
        # fields — a hostile or buggy client, or just a stale form.
        await ls.update(
            ADMIN,
            _payload(stripe_coupon_id="hijack", stripe_promotion_code_id="hijack", stripe_promotion_code="HIJACK"),
        )
    written = put_mock.call_args.args[0]
    assert written["mint_nonce"] == 3
    assert written["stripe_coupon_id"] == "coupon_real"
    assert written["stripe_promotion_code_id"] == "promo_real"
    assert written["stripe_promotion_code"] == "LIGHTBULB-REAL01"


# ---------------------------------------------------------------------------
# create_stripe_coupon() — one-click Stripe coupon + promotion code mint
# ---------------------------------------------------------------------------


def _effective_row(**over):
    row = dict(ls.DEFAULTS, competition_start_at=GOOD_START, comp_slots=220)
    row.update(over)
    return row


async def test_create_stripe_coupon_conflicts_when_already_configured():
    with patch.object(ls, "effective", AsyncMock(return_value=_effective_row(stripe_coupon_id="coupon_old"))):
        with pytest.raises(ls.CouponAlreadyConfigured):
            await ls.create_stripe_coupon(ADMIN)


async def test_create_stripe_coupon_requires_competition_start_at():
    with patch.object(ls, "effective", AsyncMock(return_value=_effective_row(competition_start_at=None))):
        with pytest.raises(ls.SettingsInvalid) as exc:
            await ls.create_stripe_coupon(ADMIN)
    assert "competition_start_at" in exc.value.errors


async def test_create_stripe_coupon_refuses_a_start_date_already_passed():
    """The promo code expires AT competition start, so a start in the past
    mints a code nobody can redeem — and Stripe rejects the past expires_at
    anyway. A field error names the knob; a bare 502 wouldn't."""
    past = "2020-01-01T00:00:00-05:00"
    with (
        patch.object(ls, "effective", AsyncMock(return_value=_effective_row(competition_start_at=past))),
        patch("stripe.Coupon.create", side_effect=AssertionError("must not reach Stripe")),
    ):
        with pytest.raises(ls.SettingsInvalid) as exc:
            await ls.create_stripe_coupon(ADMIN)
    assert "must be in the future" in exc.value.errors["competition_start_at"]


def _expected_code(env: str, start_at: str, nonce: int) -> str:
    import hashlib

    return "LIGHTBULB-" + hashlib.sha256(f"{env}:{start_at}:{nonce}".encode()).hexdigest()[:6].upper()


async def _mint(row, *, force=False, coupon_id="coupon_new", promo_id="promo_new"):
    """Drive create_stripe_coupon() against a fixed `row` (mocking `effective`
    and `put`), returning the recorded Coupon.create / PromotionCode.create
    kwargs plus what got written to the repo."""
    promo_code_holder: dict = {}

    def _make_promo(**kwargs):
        promo_code_holder["code"] = kwargs["code"]
        return SimpleNamespace(id=promo_id, code=kwargs["code"])

    put_mock = AsyncMock(side_effect=lambda item: item)
    with (
        patch.object(ls, "effective", AsyncMock(return_value=row)),
        patch.object(ls, "_clear_caches"),
        patch("stripe.Coupon.create", return_value=SimpleNamespace(id=coupon_id)) as coupon_create,
        patch("stripe.PromotionCode.create", side_effect=_make_promo) as promo_create,
        patch.object(ls.launch_settings_repo, "put", put_mock),
    ):
        result = await ls.create_stripe_coupon(ADMIN, force=force)
    return result, coupon_create, promo_create, put_mock


async def test_create_stripe_coupon_force_overrides_an_existing_coupon():
    row = _effective_row(stripe_coupon_id="coupon_old", comp_slots=50, mint_nonce=1)
    result, coupon_create, promo_create, put_mock = await _mint(row, force=True)

    assert result == {
        "stripe_coupon_id": "coupon_new",
        "stripe_promotion_code_id": "promo_new",
        "stripe_promotion_code": _expected_code(ls.settings.ENVIRONMENT, GOOD_START, 2),
    }
    coupon_kwargs = coupon_create.call_args.kwargs
    assert coupon_kwargs["percent_off"] == 100 and coupon_kwargs["duration"] == "once"
    assert coupon_kwargs["idempotency_key"] == f"launch-coupon:{ls.settings.ENVIRONMENT}:{GOOD_START}:2"

    promo_kwargs = promo_create.call_args.kwargs
    assert promo_kwargs["coupon"] == "coupon_new"
    assert promo_kwargs["max_redemptions"] == 50  # row["comp_slots"], not DEFAULTS'
    assert promo_kwargs["expires_at"] == int(ls.parse_iso(GOOD_START).timestamp())
    assert promo_kwargs["idempotency_key"] == f"launch-promo:{ls.settings.ENVIRONMENT}:{GOOD_START}:50:2"
    code = promo_kwargs["code"]
    assert code.startswith("LIGHTBULB-") and len(code) == len("LIGHTBULB-") + 6
    assert code[len("LIGHTBULB-") :].isalnum() and code[len("LIGHTBULB-") :].isupper()

    written = put_mock.call_args.args[0]
    assert written["mint_nonce"] == 2  # force bumps 1 -> 2, never re-derives 1
    assert written["stripe_coupon_id"] == "coupon_new"
    assert written["stripe_promotion_code_id"] == "promo_new"
    assert written["stripe_promotion_code"] == code
    assert written["updated_by"] == "user_admin"


async def test_create_stripe_coupon_first_mint_uses_nonce_one():
    row = _effective_row()  # mint_nonce defaults to 0 (never minted)
    result, coupon_create, promo_create, put_mock = await _mint(row)
    assert coupon_create.call_args.kwargs["idempotency_key"].endswith(":1")
    assert promo_create.call_args.kwargs["idempotency_key"].endswith(":1")
    assert put_mock.call_args.args[0]["mint_nonce"] == 1


async def test_create_stripe_coupon_retry_after_a_partial_failure_is_byte_identical():
    """A first attempt that crashed between Coupon.create and
    PromotionCode.create never persisted ids/nonce, so `stripe_coupon_id` is
    still empty — the retry must compute the SAME nonce (not a fresh one) so
    both idempotency keys, and the deterministic code, match the first
    attempt exactly and Stripe treats it as a replay, not a conflict."""
    row = _effective_row(mint_nonce=0)  # nothing persisted yet — matches a crashed first attempt
    _, first_coupon, first_promo, _ = await _mint(row)
    _, retry_coupon, retry_promo, retry_put = await _mint(row)  # same starting row — the retry
    assert first_coupon.call_args.kwargs["idempotency_key"] == retry_coupon.call_args.kwargs["idempotency_key"]
    assert first_promo.call_args.kwargs["idempotency_key"] == retry_promo.call_args.kwargs["idempotency_key"]
    assert first_promo.call_args.kwargs["code"] == retry_promo.call_args.kwargs["code"]
    assert retry_put.call_args.args[0]["mint_nonce"] == 1


async def test_create_stripe_coupon_comp_slots_change_only_moves_the_promo_key():
    """comp_slots changed between the failed attempt and the retry (same
    nonce, same start) — the coupon key/code must stay identical (no Stripe
    400 for replaying it), but the promo key must differ so Stripe treats the
    new max_redemptions as a fresh create instead of a conflicting replay."""
    row = _effective_row(mint_nonce=0, comp_slots=220)
    _, first_coupon, first_promo, _ = await _mint(row)
    row2 = _effective_row(mint_nonce=0, comp_slots=300)
    _, retry_coupon, retry_promo, _ = await _mint(row2)
    assert first_coupon.call_args.kwargs["idempotency_key"] == retry_coupon.call_args.kwargs["idempotency_key"]
    assert (
        first_promo.call_args.kwargs["code"] == retry_promo.call_args.kwargs["code"]
    )  # code doesn't depend on comp_slots
    assert first_promo.call_args.kwargs["idempotency_key"] != retry_promo.call_args.kwargs["idempotency_key"]
    assert retry_promo.call_args.kwargs["max_redemptions"] == 300


async def test_create_stripe_coupon_stripe_error_raises_without_leaking_the_message():
    row = _effective_row()
    with (
        patch.object(ls, "effective", AsyncMock(return_value=row)),
        patch("stripe.Coupon.create", side_effect=stripe.StripeError("card blocked: super secret detail")),
        patch.object(ls.launch_settings_repo, "put", AsyncMock()) as put_mock,
        patch.object(ls, "_clear_caches") as clear,
    ):
        with pytest.raises(ls.StripeMintError) as exc:
            await ls.create_stripe_coupon(ADMIN)
    # The real Stripe error is logged (module docstring/`create_stripe_coupon`),
    # never carried onto the exception the router turns into a bare 502.
    assert "super secret detail" not in str(exc.value)
    put_mock.assert_not_called()
    clear.assert_not_called()
