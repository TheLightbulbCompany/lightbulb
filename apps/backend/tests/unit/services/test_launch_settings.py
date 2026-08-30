from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_settings as ls

GOOD_DROP = "2026-09-18T00:00:00-04:00"
GOOD_START = "2026-09-21T00:00:00-04:00"


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
    }


def test_validate_accepts_a_full_valid_payload():
    row = ls.validate(_payload())
    assert row["enabled"] is True
    assert row["drop_at"] == GOOD_DROP and row["competition_start_at"] == GOOD_START
    assert row["comp_slots"] == 220 and row["competition_days"] == 30
    assert row["pot_front_cents"] == 100_000 and row["pot_per_entrant_cents"] == 1_000
    assert row["stripe_coupon_id"] == "coupon_1"


def test_validate_fills_missing_fields_from_defaults():
    assert ls.validate({}) == ls.DEFAULTS


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
