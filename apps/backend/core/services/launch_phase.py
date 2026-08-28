"""Launch Competition schedule — pure functions over settings, no I/O.

Phase ladder: waitlist (before the drop) → activation (drop .. drop+window)
→ live (competition) → ended. The competition starts exactly when the
activation window closes; the free month IS the competition month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core.config import settings

Phase = Literal["waitlist", "activation", "live", "ended"]


@dataclass(frozen=True)
class Schedule:
    drop_at: datetime
    competition_start_at: datetime
    competition_end_at: datetime
    comp_slots: int
    activation_window_hours: int
    competition_days: int


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant; `Z` suffix and offsets both normalise to UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def schedule() -> Schedule | None:
    raw = (settings.LAUNCH_DROP_AT or "").strip()
    if not raw:
        return None
    drop = parse_iso(raw)
    start = drop + timedelta(hours=settings.LAUNCH_ACTIVATION_WINDOW_HOURS)
    end = start + timedelta(days=settings.LAUNCH_COMPETITION_DAYS)
    return Schedule(
        drop_at=drop,
        competition_start_at=start,
        competition_end_at=end,
        comp_slots=settings.LAUNCH_COMP_SLOTS,
        activation_window_hours=settings.LAUNCH_ACTIVATION_WINDOW_HOURS,
        competition_days=settings.LAUNCH_COMPETITION_DAYS,
    )


def phase(s: Schedule, now: datetime | None = None) -> Phase:
    now = now or datetime.now(timezone.utc)
    if now < s.drop_at:
        return "waitlist"
    if now < s.competition_start_at:
        return "activation"
    if now < s.competition_end_at:
        return "live"
    return "ended"


def pot_cents(entrants: int) -> int:
    return settings.LAUNCH_PRIZE_FRONT_CENTS + settings.LAUNCH_POT_PER_ENTRANT_CENTS * max(entrants, 0)
