"""Phase math for the Launch Competition — launch_phase reads the
launch-settings DynamoDB row through a 30s in-process cache."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.repositories import launch_settings_repo
from core.services import launch_phase

DROP = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)  # Thu Sep 17 midnight Eastern = Fri 00:00 -04:00
START = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)  # Sun Sep 20 midnight Eastern = Mon 00:00 -04:00


def _row(**over):
    row = dict(launch_settings_repo.DEFAULTS)
    row.update(over)
    return row


CONFIGURED = _row(
    enabled=True,
    drop_at="2026-09-18T00:00:00-04:00",
    competition_start_at="2026-09-21T00:00:00-04:00",
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
    row = _row(enabled=False, drop_at="2026-09-18T00:00:00-04:00", competition_start_at="2026-09-21T00:00:00-04:00")
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        assert launch_phase.schedule() is None


def test_schedule_is_none_when_a_date_is_null():
    row = _row(enabled=True, drop_at="2026-09-18T00:00:00-04:00", competition_start_at=None)
    with patch.object(launch_settings_repo, "get_sync", return_value=row):
        assert launch_phase.schedule() is None


def test_schedule_derives_end_and_window(configured):
    s = launch_phase.schedule()
    assert s.drop_at == DROP and s.competition_start_at == START
    assert s.competition_end_at == START + timedelta(days=30)
    assert s.activation_window_hours == 72 and s.comp_slots == 220


def test_schedule_treats_start_before_drop_as_feature_off_not_a_raise(caplog):
    """launch_settings.validate() already refuses this order at write time —
    this only exercises a hand-edited row. The poll path must never fail
    because of it: log and go feature-off, not raise."""
    bad = _row(enabled=True, drop_at="2026-09-21T00:00:00-04:00", competition_start_at="2026-09-18T00:00:00-04:00")
    with patch.object(launch_settings_repo, "get_sync", return_value=bad):
        with caplog.at_level("WARNING", logger=launch_phase.logger.name):
            assert launch_phase.schedule() is None
    assert "failed to derive a schedule" in caplog.text


def test_schedule_never_raises_on_a_malformed_int_field(caplog):
    """Belt-and-braces on top of _plain()'s int coercion: even if a bad type
    slips through (e.g. a hand-edited row), schedule()'s own arithmetic must
    not raise on the poll path — it logs and goes feature-off."""
    bad = _row(enabled=True, drop_at=CONFIGURED["drop_at"], competition_start_at=CONFIGURED["competition_start_at"])
    bad["competition_days"] = "x"
    with patch.object(launch_settings_repo, "get_sync", return_value=bad):
        with caplog.at_level("WARNING", logger=launch_phase.logger.name):
            assert launch_phase.schedule() is None
    assert "failed to derive a schedule" in caplog.text


@pytest.mark.parametrize(
    "now,expected",
    [
        (DROP - timedelta(days=21), "waitlist"),
        (DROP - timedelta(seconds=1), "waitlist"),
        (DROP, "activation"),
        (START - timedelta(seconds=1), "activation"),
        (START, "live"),
        (START + timedelta(days=29), "live"),
        (START + timedelta(days=30), "ended"),
    ],
)
def test_phase_boundaries(configured, now, expected):
    assert launch_phase.phase(launch_phase.schedule(), now) == expected


def test_pot_and_constants_read_the_row(configured):
    assert launch_phase.pot_cents(0) == 100_000
    assert launch_phase.pot_cents(500) == 600_000
    assert launch_phase.constants() == {
        "comp_slots": 220,
        "competition_days": 30,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
    }


def test_coupon_id_reads_the_row():
    with patch.object(launch_settings_repo, "get_sync", return_value=_row(stripe_coupon_id="promo_123")):
        assert launch_phase.coupon_id() == "promo_123"


def test_coupon_id_empty_when_unset():
    with patch.object(launch_settings_repo, "get_sync", return_value=None):
        assert launch_phase.coupon_id() == ""


_CACHE_TTL_S_PLUS = launch_phase._CACHE_TTL_S + 1


def test_cache_honours_the_ttl_then_refetches():
    """A second call inside the 30s window must not hit the repo again; once
    the TTL has elapsed the next call refetches."""
    calls = {"n": 0}
    rows = [
        CONFIGURED,
        _row(
            comp_slots=999,
            enabled=True,
            drop_at=CONFIGURED["drop_at"],
            competition_start_at=CONFIGURED["competition_start_at"],
        ),
    ]

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


def test_ddb_read_failure_after_ttl_serves_the_last_known_row(caplog):
    """The poll path must never fail because DynamoDB hiccuped — a refetch
    failure after the TTL falls back to the previously cached row, not to
    DEFAULTS, and does not raise."""
    t = {"now": time.monotonic()}
    with patch("core.services.launch_phase.time.monotonic", side_effect=lambda: t["now"]):
        with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED):
            first = launch_phase.schedule()
        assert first is not None and first.comp_slots == 220

        t["now"] += _CACHE_TTL_S_PLUS
        with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
            with caplog.at_level("WARNING", logger=launch_phase.logger.name):
                s = launch_phase.schedule()
    assert s is not None and s.comp_slots == 220
    assert "settings read failed" in caplog.text


def test_cold_cache_read_failure_is_not_cached_and_reports_unavailable():
    """A failed read with nothing cached must not become a 30s "feature off"
    answer: the DEFAULTS served are flagged as a guess (settings_unavailable),
    nothing is cached, and the very next call retries — and picks up the row
    the moment DynamoDB comes back."""
    with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
        assert launch_phase.schedule() is None
        assert launch_phase.settings_unavailable() is True
        assert launch_phase._CACHE["row"] is None  # nothing cached -> the next call retries

    with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED) as ok:
        s = launch_phase.schedule()
    assert s is not None and ok.call_count == 1
    assert launch_phase.settings_unavailable() is False


def test_read_failure_with_a_warm_cache_is_not_unavailable():
    """A cached row is a real answer, stale or not — only a cold-cache failure
    is 'unavailable'."""
    t = {"now": time.monotonic()}
    with patch("core.services.launch_phase.time.monotonic", side_effect=lambda: t["now"]):
        with patch.object(launch_settings_repo, "get_sync", return_value=CONFIGURED):
            assert launch_phase.schedule() is not None
        t["now"] += _CACHE_TTL_S_PLUS
        with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
            assert launch_phase.schedule() is not None
    assert launch_phase.settings_unavailable() is False


def test_invalidate_clears_the_unavailable_flag():
    with patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down")):
        launch_phase.schedule()
    assert launch_phase.settings_unavailable() is True
    launch_phase.invalidate()
    assert launch_phase.settings_unavailable() is False
