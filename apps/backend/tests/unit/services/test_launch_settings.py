from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import stripe

from core.auth import AuthContext
from core.services import launch_settings as ls

GOOD_CLOSES = "2026-09-30T12:00:00-04:00"
GOOD_START = "2026-10-01T00:00:00-04:00"
GOOD_END = "2026-12-01T00:00:00-05:00"
ADMIN = AuthContext(user_id="user_admin")


def _payload(**over):
    p = {
        "enabled": True,
        "sheet_closes_at": GOOD_CLOSES,
        "competition_start_at": GOOD_START,
        "competition_end_at": GOOD_END,
        "comp_slots": 200,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "referral_jump": 10,
    }
    p.update(over)
    return p


def test_defaults_shape():
    assert ls.DEFAULTS == {
        "enabled": False,
        "sheet_closes_at": None,
        "competition_start_at": None,
        "competition_end_at": None,
        "comp_slots": 200,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "pot_per_seat_cents": 500,
        "referral_jump": 10,
        "closed_at": None,
        "final_pot_cents": None,
        "final_roster_count": None,
        "close_started_at": None,
        "close_result": None,
        "stripe_coupon_id": "",
        "email_copy": {},
        "mint_nonce": 0,
    }


def test_validate_accepts_a_full_valid_payload():
    row = ls.validate(_payload())
    assert row["enabled"] is True
    assert row["sheet_closes_at"] == GOOD_CLOSES
    assert row["competition_start_at"] == GOOD_START
    assert row["competition_end_at"] == GOOD_END
    assert row["comp_slots"] == 200 and row["referral_jump"] == 10
    assert row["pot_front_cents"] == 100_000 and row["pot_per_entrant_cents"] == 1_000


@pytest.mark.parametrize(
    "field,value",
    [
        ("sheet_closes_at", "not a date at all"),
        ("sheet_closes_at", "2026-09-30T12:00:00"),  # naive — no offset
        ("competition_start_at", "2026/10/01 00:00:00-04:00"),
        ("competition_end_at", "2026-02-29T00:00:00-05:00"),  # not a leap year
    ],
)
def test_validate_rejects_bad_dates(field, value):
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(**{field: value}))
    assert field in exc.value.errors


def test_validate_rejects_start_not_after_closes():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(competition_start_at=GOOD_CLOSES))
    assert "competition_start_at" in exc.value.errors


def test_validate_rejects_end_not_after_start():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(competition_end_at=GOOD_START))
    assert "competition_end_at" in exc.value.errors


@pytest.mark.parametrize(
    "field,value",
    [
        ("comp_slots", 0),
        ("comp_slots", "a lot"),
        ("pot_front_cents", -1),
        ("pot_per_entrant_cents", -1),
        ("referral_jump", -1),
    ],
)
def test_validate_rejects_out_of_range_ints(field, value):
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(**{field: value}))
    assert field in exc.value.errors


def test_validate_allows_zero_pot_and_zero_jump():
    row = ls.validate(_payload(pot_front_cents=0, pot_per_entrant_cents=0, referral_jump=0))
    assert row["pot_front_cents"] == 0 and row["referral_jump"] == 0


@pytest.mark.parametrize("missing", ["sheet_closes_at", "competition_start_at", "competition_end_at"])
def test_validate_rejects_enabled_with_a_missing_date(missing):
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(**{missing: None}))
    assert "enabled" in exc.value.errors


def test_validate_allows_disabled_with_no_dates():
    row = ls.validate(_payload(enabled=False, sheet_closes_at=None, competition_start_at=None, competition_end_at=None))
    assert row["enabled"] is False and row["sheet_closes_at"] is None


def test_validate_collects_multiple_field_errors_at_once():
    with pytest.raises(ls.SettingsInvalid) as exc:
        ls.validate(_payload(comp_slots=-1, referral_jump=-1, sheet_closes_at="garbage"))
    assert {"comp_slots", "referral_jump", "sheet_closes_at"} <= set(exc.value.errors)


def test_to_admin_drops_only_mint_nonce():
    row = dict(
        ls.DEFAULTS,
        updated_at="2026-08-29T00:00:00+00:00",
        updated_by="user_admin",
        mint_nonce=3,
        closed_at="2026-09-30T16:00:00+00:00",
        final_pot_cents=250_000,
    )
    admin = ls.to_admin(row)
    assert "mint_nonce" not in admin
    assert admin["closed_at"] == "2026-09-30T16:00:00+00:00"
    assert admin["final_pot_cents"] == 250_000
    assert admin["updated_by"] == "user_admin"


async def test_effective_merges_defaults_with_the_stored_row():
    with patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value={"comp_slots": 42})):
        row = await ls.effective()
    assert row["comp_slots"] == 42  # from the stored row
    assert row["referral_jump"] == 10  # from DEFAULTS — not stored


# ---------------------------------------------------------------------------
# update() — validate, persist, clear caches, server-managed carry-forward
# ---------------------------------------------------------------------------


async def test_update_persists_the_validated_row_stamped_with_the_writer():
    put_mock = AsyncMock(side_effect=lambda item: {**item, "updated_at": "2026-08-29T00:00:00+00:00"})
    with (
        patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=None)),
        patch.object(ls.launch_settings_repo, "put", put_mock),
        patch.object(ls, "_clear_caches") as clear,
    ):
        admin_row = await ls.update(ADMIN, _payload())
    assert admin_row["enabled"] is True
    assert "mint_nonce" not in admin_row  # to_admin strips it — server-only
    written = put_mock.call_args.args[0]
    assert written["updated_by"] == "user_admin"
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


async def test_update_preserves_server_managed_fields_regardless_of_payload():
    """A settings save can never blank the minted coupon, the idempotency
    nonce, or the close state — carried forward from the STORED row, not
    trusted from the PUT body."""
    stored = {
        **ls.DEFAULTS,
        "mint_nonce": 3,
        "stripe_coupon_id": "coupon_real",
        "closed_at": "2026-09-30T16:00:00+00:00",
        "final_pot_cents": 250_000,
        "final_roster_count": 150,
        "close_started_at": "2026-09-30T15:59:00+00:00",
        "close_result": {"winners": 2},
    }
    put_mock = AsyncMock(side_effect=lambda item: item)
    with (
        patch.object(ls.launch_settings_repo, "get", AsyncMock(return_value=stored)),
        patch.object(ls.launch_settings_repo, "put", put_mock),
        patch.object(ls, "_clear_caches"),
    ):
        await ls.update(ADMIN, _payload(stripe_coupon_id="hijack", closed_at=None, final_pot_cents=0))
    written = put_mock.call_args.args[0]
    assert written["mint_nonce"] == 3
    assert written["stripe_coupon_id"] == "coupon_real"
    assert written["closed_at"] == "2026-09-30T16:00:00+00:00"
    assert written["final_pot_cents"] == 250_000
    assert written["final_roster_count"] == 150
    assert written["close_started_at"] == "2026-09-30T15:59:00+00:00"
    assert written["close_result"] == {"winners": 2}


async def test_record_close_state_is_an_atomic_partial_update():
    """A single UpdateExpression SET of ONLY the supplied fields — no read,
    no full-row put, so it can never clobber a concurrent admin save."""
    update_mock = AsyncMock()
    with (
        patch.object(ls.launch_settings_repo, "update_fields", update_mock),
        patch.object(ls.launch_settings_repo, "put", AsyncMock()) as put_mock,
        patch.object(ls.launch_settings_repo, "get", AsyncMock()) as get_mock,
        patch.object(ls, "_clear_caches") as clear,
    ):
        await ls.record_close_state({"closed_at": "2026-09-30T16:00:00+00:00"})
    update_mock.assert_awaited_once_with(
        {"closed_at": "2026-09-30T16:00:00+00:00"}, condition=None, condition_values=None
    )
    put_mock.assert_not_awaited()
    get_mock.assert_not_awaited()
    clear.assert_called_once()


# ---------------------------------------------------------------------------
# create_stripe_coupon() — one coupon, no promotion codes
# ---------------------------------------------------------------------------


def _effective_row(**over):
    row = dict(ls.DEFAULTS)
    row.update(over)
    return row


async def _mint(row, *, force=False, coupon_id="coupon_new"):
    put_mock = AsyncMock(side_effect=lambda item: item)
    with (
        patch.object(ls, "effective", AsyncMock(return_value=row)),
        patch.object(ls, "_clear_caches"),
        patch.object(ls.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_flat"),
        patch("stripe.Price.retrieve", return_value={"product": "prod_flat"}),
        patch("stripe.Coupon.create", return_value=SimpleNamespace(id=coupon_id)) as coupon_create,
        patch.object(ls.launch_settings_repo, "put", put_mock),
    ):
        result = await ls.create_stripe_coupon(ADMIN, force=force)
    return result, coupon_create, put_mock


@pytest.fixture
def coupon_still_redeemable(monkeypatch):
    # The real deadline is a date: minting is refused once it has passed, and these tests are about minting.
    monkeypatch.setattr(ls, "COUPON_REDEEM_BY", datetime(2099, 1, 1, tzinfo=timezone.utc))


async def test_create_stripe_coupon_conflicts_when_already_configured():
    with patch.object(ls, "effective", AsyncMock(return_value=_effective_row(stripe_coupon_id="coupon_old"))):
        with pytest.raises(ls.CouponAlreadyConfigured):
            await ls.create_stripe_coupon(ADMIN)


async def test_create_stripe_coupon_mints_100_off_once_with_redeem_by(coupon_still_redeemable):
    result, coupon_create, put_mock = await _mint(_effective_row())
    assert result == {"stripe_coupon_id": "coupon_new"}
    kwargs = coupon_create.call_args.kwargs
    assert kwargs["percent_off"] == 100 and kwargs["duration"] == "once"
    assert kwargs["redeem_by"] == int(ls.COUPON_REDEEM_BY.timestamp())
    # Product-restricted: the free month discounts the Workspace subscription
    # line ONLY — never the bundled starter-credits line item.
    assert kwargs["applies_to"] == {"products": ["prod_flat"]}
    assert kwargs["idempotency_key"] == f"launch-coupon:{ls.settings.ENVIRONMENT}:prod_flat:1"
    written = put_mock.call_args.args[0]
    assert written["mint_nonce"] == 1
    assert written["stripe_coupon_id"] == "coupon_new"
    assert written["updated_by"] == "user_admin"
    assert "stripe_promotion_code_id" not in written  # promotion codes are dead


async def test_create_stripe_coupon_force_bumps_the_nonce(coupon_still_redeemable):
    row = _effective_row(stripe_coupon_id="coupon_old", mint_nonce=1)
    _, coupon_create, put_mock = await _mint(row, force=True)
    assert coupon_create.call_args.kwargs["idempotency_key"].endswith(":prod_flat:2")
    assert put_mock.call_args.args[0]["mint_nonce"] == 2


async def test_create_stripe_coupon_retry_after_a_partial_failure_is_byte_identical(coupon_still_redeemable):
    """A first attempt that crashed after Coupon.create never persisted the
    id/nonce — the retry must compute the SAME nonce so the idempotency key
    matches and Stripe replays instead of conflicting."""
    row = _effective_row(mint_nonce=0)
    _, first, _ = await _mint(row)
    _, retry, retry_put = await _mint(row)
    assert first.call_args.kwargs["idempotency_key"] == retry.call_args.kwargs["idempotency_key"]
    assert retry_put.call_args.args[0]["mint_nonce"] == 1


async def test_create_stripe_coupon_requires_the_flat_price_id():
    with (
        patch.object(ls, "effective", AsyncMock(return_value=_effective_row())),
        patch.object(ls.settings, "STRIPE_WORKSPACE_PRICE_ID", ""),
    ):
        with pytest.raises(ls.SettingsInvalid):
            await ls.create_stripe_coupon(ADMIN)


async def test_create_stripe_coupon_stripe_error_raises_without_leaking_the_message(coupon_still_redeemable):
    with (
        patch.object(ls, "effective", AsyncMock(return_value=_effective_row())),
        patch.object(ls.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_flat"),
        patch("stripe.Price.retrieve", return_value={"product": "prod_flat"}),
        patch("stripe.Coupon.create", side_effect=stripe.StripeError("card blocked: super secret detail")),
        patch.object(ls.launch_settings_repo, "put", AsyncMock()) as put_mock,
        patch.object(ls, "_clear_caches") as clear,
    ):
        with pytest.raises(ls.StripeMintError) as exc:
            await ls.create_stripe_coupon(ADMIN)
    assert "super secret detail" not in str(exc.value)
    put_mock.assert_not_called()
    clear.assert_not_called()


# ---------------------------------------------------------------------------
# Admin-editable email copy
# ---------------------------------------------------------------------------


def test_email_copy_validate_accepts_supported_placeholders():
    out = ls.validate_email_copy("competing", {"lead": "  {comp_slots} months  ", "detail": "plain"})
    assert out == {"lead": "{comp_slots} months", "detail": "plain"}


def test_email_copy_validate_drops_blank_fields_so_they_fall_back_to_the_default():
    assert ls.validate_email_copy("not_selected", {"subject": "", "lead": "   ", "detail": None}) == {}


def test_email_copy_validate_rejects_a_placeholder_the_variant_cannot_supply():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("winner_paid", {"detail": "hi {comp_slots}"})
    assert "comp_slots" in e.value.errors["detail"]
    assert "supports none" in e.value.errors["detail"]


def test_email_copy_validate_rejects_a_typo_placeholder():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("competing", {"lead": "{compslots}"})
    assert "{compslots}" in e.value.errors["lead"]


def test_email_copy_validate_rejects_unbalanced_braces():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("competing", {"lead": "half {open"})
    assert "unbalanced" in e.value.errors["lead"]


def test_email_copy_validate_rejects_an_unknown_variant():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("nope", {"lead": "x"})
    assert "variant" in e.value.errors


def test_email_copy_validate_caps_length():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("competing", {"detail": "x" * (ls.MAX_COPY_LEN + 1)})
    assert "at most" in e.value.errors["detail"]


async def test_update_email_copy_writes_only_that_variant_and_clears_caches():
    written: list[dict] = []

    async def _update_fields(fields, **kw):
        written.append(fields)
        return True

    with (
        patch.object(ls, "effective", AsyncMock(return_value={"email_copy": {"join": {"lead": "keep me"}}})),
        patch.object(ls.launch_settings_repo, "update_fields", AsyncMock(side_effect=_update_fields)),
        patch.object(ls, "_clear_caches") as clear,
    ):
        copy = await ls.update_email_copy(ADMIN, "competing", {"lead": "new lead"})
    assert copy == {"join": {"lead": "keep me"}, "competing": {"lead": "new lead"}}
    assert written[0]["email_copy"] == copy and written[0]["updated_by"] == "user_admin"
    clear.assert_called_once()


async def test_update_email_copy_with_every_field_blank_resets_to_default():
    with (
        patch.object(ls, "effective", AsyncMock(return_value={"email_copy": {"competing": {"lead": "old"}}})),
        patch.object(ls.launch_settings_repo, "update_fields", AsyncMock(return_value=True)),
        patch.object(ls, "_clear_caches"),
    ):
        copy = await ls.update_email_copy(ADMIN, "competing", {"subject": "", "lead": "", "detail": ""})
    assert copy == {}  # the row no longer overrides it — the module default is back


async def test_a_settings_save_carries_the_email_copy_forward():
    """The settings PUT is a full replace of eight fields; without the
    carry-forward every schedule edit would silently wipe the admin's copy."""
    stored: dict = {}

    async def _put(item):
        stored.update(item)
        return dict(item)

    current = {**ls.DEFAULTS, "email_copy": {"competing": {"lead": "hand-written"}}}
    with (
        patch.object(ls, "effective", AsyncMock(return_value=current)),
        patch.object(ls.launch_settings_repo, "put", AsyncMock(side_effect=_put)),
        patch.object(ls, "_clear_caches"),
    ):
        saved = await ls.update(ADMIN, _payload())
    assert stored["email_copy"] == {"competing": {"lead": "hand-written"}}
    assert saved["email_copy"] == {"competing": {"lead": "hand-written"}}


def test_email_copy_validate_rejects_attribute_traversal():
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("competing", {"lead": "{comp_slots.__class__}"})
    assert "attribute and index" in e.value.errors["lead"]


def test_email_copy_validate_rejects_a_format_spec():
    """A width spec is a string-amplification bomb — it renders before
    `_fill`'s except can help, on the one backend task."""
    with pytest.raises(ls.SettingsInvalid) as e:
        ls.validate_email_copy("competing", {"detail": "{comp_slots:>999999999}"})
    assert "format specs" in e.value.errors["detail"]


def test_email_copy_validate_rejects_a_multiline_subject():
    """A newline in a subject makes EmailMessage RAISE at send: the whole
    cohort would land in email_failures with nothing saying why."""
    for bad in ("two\nlines", "carriage\rreturn"):
        with pytest.raises(ls.SettingsInvalid) as e:
            ls.validate_email_copy("competing", {"subject": bad})
        assert "single line" in e.value.errors["subject"]
    # …but a newline in the BODY fields is fine.
    assert ls.validate_email_copy("competing", {"detail": "two\nlines"}) == {"detail": "two\nlines"}
