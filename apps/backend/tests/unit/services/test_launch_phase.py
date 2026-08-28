"""Phase math for the Launch Competition — pure functions over settings."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.services import launch_phase

DROP = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def configured():
    with (
        patch.object(launch_phase.settings, "LAUNCH_DROP_AT", "2026-09-18T16:00:00Z"),
        patch.object(launch_phase.settings, "LAUNCH_ACTIVATION_WINDOW_HOURS", 72),
        patch.object(launch_phase.settings, "LAUNCH_COMPETITION_DAYS", 30),
        patch.object(launch_phase.settings, "LAUNCH_COMP_SLOTS", 220),
    ):
        yield


def test_schedule_is_none_when_unset():
    with patch.object(launch_phase.settings, "LAUNCH_DROP_AT", ""):
        assert launch_phase.schedule() is None


def test_schedule_derives_start_and_end(configured):
    s = launch_phase.schedule()
    assert s.drop_at == DROP
    assert s.competition_start_at == DROP + timedelta(hours=72)
    assert s.competition_end_at == DROP + timedelta(hours=72, days=30)
    assert s.comp_slots == 220


@pytest.mark.parametrize(
    "now,expected",
    [
        (DROP - timedelta(days=21), "waitlist"),
        (DROP - timedelta(seconds=1), "waitlist"),
        (DROP, "activation"),
        (DROP + timedelta(hours=71, minutes=59), "activation"),
        (DROP + timedelta(hours=72), "live"),
        (DROP + timedelta(hours=72, days=29), "live"),
        (DROP + timedelta(hours=72, days=30), "ended"),
    ],
)
def test_phase_boundaries(configured, now, expected):
    assert launch_phase.phase(launch_phase.schedule(), now) == expected


def test_pot_is_front_plus_per_entrant():
    with (
        patch.object(launch_phase.settings, "LAUNCH_PRIZE_FRONT_CENTS", 100_000),
        patch.object(launch_phase.settings, "LAUNCH_POT_PER_ENTRANT_CENTS", 1_000),
    ):
        assert launch_phase.pot_cents(0) == 100_000
        assert launch_phase.pot_cents(500) == 600_000


def test_parse_iso_accepts_z_and_offset():
    assert launch_phase.parse_iso("2026-09-18T16:00:00Z") == DROP
    assert launch_phase.parse_iso("2026-09-18T12:00:00-04:00") == DROP
