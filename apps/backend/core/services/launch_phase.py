"""Launch Competition schedule — pure functions over launch-schedule.json, no I/O after load.

launch-schedule.json at the repo root is THE source of the dates and pot
constants for every surface (backend runtime here; frontend build time via
apps/frontend/src/lib/launchSchedule.ts). Keyed per environment like
openclaw-version.json. null dates = feature off.

Phase ladder: waitlist (before the drop) → activation (drop .. competition
start) → live → ended.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from core.config import settings

Phase = Literal["waitlist", "activation", "live", "ended"]
FILENAME = "launch-schedule.json"
_FILE: dict | None = None


@dataclass(frozen=True)
class Schedule:
    drop_at: datetime
    competition_start_at: datetime
    competition_end_at: datetime
    comp_slots: int
    activation_window_hours: int
    competition_days: int


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_schedule_file() -> dict:
    """Read launch-schedule.json by searching upward from this file (same
    posture as core/openclaw_version.py — the backend image copies the file
    into its build context; a hardcoded parents[N] would break in the container)."""
    global _FILE
    if _FILE is not None:
        return _FILE
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / FILENAME
        if candidate.is_file():
            _FILE = json.loads(candidate.read_text())
            return _FILE
    raise FileNotFoundError(FILENAME)


def env_key() -> str:
    return "prod" if settings.ENVIRONMENT == "prod" else "dev"


def constants() -> dict:
    f = load_schedule_file()
    return {
        "comp_slots": int(f["comp_slots"]),
        "competition_days": int(f["competition_days"]),
        "pot_front_cents": int(f["pot_front_cents"]),
        "pot_per_entrant_cents": int(f["pot_per_entrant_cents"]),
    }


def schedule() -> Schedule | None:
    f = load_schedule_file()
    env = f[env_key()]
    if not env.get("drop_at") or not env.get("competition_start_at"):
        return None
    drop = parse_iso(env["drop_at"])
    start = parse_iso(env["competition_start_at"])
    if start <= drop:
        raise ValueError("launch-schedule.json: competition_start_at must be after drop_at")
    c = constants()
    return Schedule(
        drop_at=drop,
        competition_start_at=start,
        competition_end_at=start + timedelta(days=c["competition_days"]),
        comp_slots=c["comp_slots"],
        activation_window_hours=int((start - drop).total_seconds() // 3600),
        competition_days=c["competition_days"],
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
    c = constants()
    return c["pot_front_cents"] + c["pot_per_entrant_cents"] * max(entrants, 0)
