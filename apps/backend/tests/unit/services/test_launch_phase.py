"""Phase math for the Launch Competition — pure functions over launch-schedule.json."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.services import launch_phase

DROP = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)  # Thu Sep 17 midnight Eastern = Fri 00:00 -04:00
START = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)  # Sun Sep 20 midnight Eastern = Mon 00:00 -04:00


def _file(dev=None, prod=None, **over):
    base = {
        "competition_days": 30,
        "comp_slots": 220,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
        "dev": dev or {"drop_at": None, "competition_start_at": None},
        "prod": prod or {"drop_at": None, "competition_start_at": None},
    }
    base.update(over)
    return base


CONFIGURED = _file(dev={"drop_at": "2026-09-18T00:00:00-04:00", "competition_start_at": "2026-09-21T00:00:00-04:00"})


@pytest.fixture
def configured():
    with patch.object(launch_phase, "_FILE", CONFIGURED), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        yield


def test_schedule_is_none_when_env_dates_are_null():
    with patch.object(launch_phase, "_FILE", _file()), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        assert launch_phase.schedule() is None


def test_schedule_selects_the_env_key():
    prod_only = _file(
        prod={"drop_at": "2026-09-18T00:00:00-04:00", "competition_start_at": "2026-09-21T00:00:00-04:00"}
    )
    with patch.object(launch_phase, "_FILE", prod_only):
        with patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
            assert launch_phase.schedule() is None
        with patch.object(launch_phase.settings, "ENVIRONMENT", "prod"):
            assert launch_phase.schedule().drop_at == DROP


def test_schedule_derives_end_and_window(configured):
    s = launch_phase.schedule()
    assert s.drop_at == DROP and s.competition_start_at == START
    assert s.competition_end_at == START + timedelta(days=30)
    assert s.activation_window_hours == 72 and s.comp_slots == 220


def test_schedule_rejects_start_before_drop():
    bad = _file(dev={"drop_at": "2026-09-21T00:00:00-04:00", "competition_start_at": "2026-09-18T00:00:00-04:00"})
    with patch.object(launch_phase, "_FILE", bad), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        with pytest.raises(ValueError):
            launch_phase.schedule()


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


def test_pot_reads_the_file(configured):
    assert launch_phase.pot_cents(0) == 100_000
    assert launch_phase.pot_cents(500) == 600_000
    assert launch_phase.constants() == {
        "comp_slots": 220,
        "competition_days": 30,
        "pot_front_cents": 100_000,
        "pot_per_entrant_cents": 1_000,
    }


def test_real_file_parses_and_is_off_by_default():
    f = launch_phase.load_schedule_file()
    for env in ("dev", "prod"):
        assert set(f[env]) == {"drop_at", "competition_start_at"}
        if f[env]["drop_at"] is not None:
            assert launch_phase.parse_iso(f[env]["competition_start_at"]) > launch_phase.parse_iso(f[env]["drop_at"])
