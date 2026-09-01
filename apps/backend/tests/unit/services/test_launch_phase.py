"""Phase math for the Launch Competition sign-up sheet — launch_phase reads
the launch-settings DynamoDB row through a 30s in-process cache."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.repositories import launch_settings_repo
from core.services import launch_phase

CLOSES = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)  # Sept 30 noon ET
START = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
END = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)


def _row(**over):
    row = dict(launch_settings_repo.DEFAULTS)
    row.update(over)
    return row


CONFIGURED = _row(
    enabled=True,
    sheet_closes_at="2026-09-30T12:00:00-04:00",
    competition_start_at="2026-10-01T00:00:00-04:00",
    competition_end_at="2026-12-01T00:00:00-05:00",
)


@pytest.fixture(autouse=True)
def _reset_cache():
    launch_phase.invalidate()
    yield
    launch_phase.invalidate()


@pytest.fixture
def configured():
    with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED):
        yield


def test_schedule_is_none_when_no_row_saved_yet():
    with patch.object(launch_settings_repo, "get_sync", return_value=None):
        assert launch_phase.schedule() is None


def test_schedule_is_none_when_disabled():
    row = dict(CONFIGURED, enabled=False)
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        assert launch_phase.schedule() is None


@pytest.mark.parametrize("missing", ["sheet_closes_at", "competition_start_at", "competition_end_at"])
def test_schedule_is_none_when_any_date_is_null(missing):
    row = dict(CONFIGURED)
    row[missing] = None
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        assert launch_phase.schedule() is None


def test_schedule_reads_the_three_explicit_dates(configured):
    s = launch_phase.schedule()
    assert s.sheet_closes_at == CLOSES
    assert s.competition_start_at == START
    assert s.competition_end_at == END
    assert s.comp_slots == 200 and s.closed_at is None


def test_schedule_carries_closed_at():
    row = dict(CONFIGURED, closed_at="2026-09-28T09:00:00-04:00")
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        s = launch_phase.schedule()
    assert s.closed_at == datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "over",
    [
        # start before closes
        {"competition_start_at": "2026-09-29T00:00:00-04:00"},
        # end before start
        {"competition_end_at": "2026-09-30T13:00:00-04:00"},
        # garbage date type
        {"competition_end_at": 12345},
    ],
)
def test_schedule_treats_a_malformed_row_as_feature_off_not_a_raise(caplog, over):
    """launch_settings.validate() already refuses these at write time — this
    only exercises a hand-edited row. Log and go feature-off, never a 500."""
    bad = dict(CONFIGURED, **over)
    with patch.object(launch_settings_repo, "get_sync", return_value=bad):
        with caplog.at_level("WARNING", logger=launch_phase.logger.name):
            assert launch_phase.schedule() is None
    assert "failed to derive a schedule" in caplog.text


@pytest.mark.parametrize(
    "now,expected",
    [
        (CLOSES - timedelta(days=21), "open"),
        (CLOSES - timedelta(seconds=1), "open"),
        (CLOSES, "closed"),
        (START - timedelta(seconds=1), "closed"),
        (START, "competition"),
        (END - timedelta(seconds=1), "competition"),
        (END, "ended"),
    ],
)
def test_phase_boundaries(configured, now, expected):
    assert launch_phase.phase(launch_phase.schedule(), now) == expected


def test_early_close_via_closed_at_leaves_open_before_sheet_closes_at():
    """The admin close action stamps closed_at; the phase must leave "open"
    immediately even though sheet_closes_at is still in the future."""
    row = dict(CONFIGURED, closed_at="2026-09-20T09:00:00-04:00")
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        s = launch_phase.schedule()
    assert launch_phase.phase(s, CLOSES - timedelta(days=5)) == "closed"
    # ...and the later boundaries are untouched.
    assert launch_phase.phase(s, START) == "competition"
    assert launch_phase.phase(s, END) == "ended"


def test_pot_and_constants_read_the_row(configured):
    assert launch_phase.pot_cents(0) == 100_000
    assert launch_phase.pot_cents(500) == 600_000
    assert launch_phase.constants() == {
        "comp_slots": 200,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "pot_per_seat_cents": 500,
        "referral_jump": 10,
    }


def test_coupon_id_reads_the_row():
    with patch.object(launch_settings_repo, "get_sync", return_value=_row(stripe_coupon_id="coup_123")):
        assert launch_phase.coupon_id() == "coup_123"


def test_coupon_id_empty_when_unset():
    with patch.object(launch_settings_repo, "get_sync", return_value=None):
        assert launch_phase.coupon_id() == ""


_CACHE_TTL_S_PLUS = launch_phase._CACHE_TTL_S + 1


def test_cache_honours_the_ttl_then_refetches():
    calls = {"n": 0}
    rows = [CONFIGURED, dict(CONFIGURED, comp_slots=999)]

    def _get_sync():
        calls["n"] += 1
        return rows[min(calls["n"] - 1, len(rows) - 1)]

    t = {"now": time.monotonic()}

    with (
        patch.object(launch_settings_repo, "get_sync", side_effect=_get_sync),
        patch("core.services.launch_phase.time.monotonic", side_effect=lambda: t["now"]),
    ):
        launch_phase.schedule()
        launch_phase.schedule()
        assert calls["n"] == 1  # served from cache, no second repo hit

        t["now"] += _CACHE_TTL_S_PLUS
        s = launch_phase.schedule()
        assert calls["n"] == 2 and s.comp_slots == 999  # TTL elapsed -> refetched


def test_invalidate_forces_a_refetch():
    calls = {"n": 0}

    def _get_sync():
        calls["n"] += 1
        return CONFIGURED

    with patch.object(launch_settings_repo, "get_sync", side_effect=_get_sync):
        launch_phase.schedule()
        launch_phase.schedule()
        assert calls["n"] == 1

        launch_phase.invalidate()
        launch_phase.schedule()
        assert calls["n"] == 2


def test_ddb_read_failure_on_a_cold_cache_falls_back_to_defaults_feature_off(caplog):
    with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
        with caplog.at_level("WARNING", logger=launch_phase.logger.name):
            assert launch_phase.schedule() is None  # DEFAULTS: enabled=False
    assert "settings read failed" in caplog.text
    # Nothing cached -> the very next call retries and picks the row up.
    assert launch_phase._CACHE["row"] is None
    with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED) as ok:
        assert launch_phase.schedule() is not None
    assert ok.call_count == 1


def test_ddb_read_failure_after_ttl_serves_the_last_known_row(caplog):
    t = {"now": time.monotonic()}
    with patch("core.services.launch_phase.time.monotonic", side_effect=lambda: t["now"]):
        with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED):
            assert launch_phase.schedule() is not None

        t["now"] += _CACHE_TTL_S_PLUS
        with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
            with caplog.at_level("WARNING", logger=launch_phase.logger.name):
                s = launch_phase.schedule()
    assert s is not None and s.comp_slots == 200
    assert "settings read failed" in caplog.text
