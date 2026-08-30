"""Launch Competition schedule — reads the launch-settings DynamoDB row
through a 30-second in-process cache.

The row (``core.repositories.launch_settings_repo``, edited via
``core.services.launch_settings`` from the admin console) is THE source of
the dates and pot constants for every surface. This module sits on the
container-status poll path (``core.services.launch_hold``), so it caches the
row and never lets a DynamoDB hiccup fail that path — see ``_row()``.
``enabled=False`` or either date missing = feature off.

Phase ladder: waitlist (before the drop) → activation (drop .. competition
start) → live → ended.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core.repositories import launch_settings_repo

logger = logging.getLogger(__name__)

Phase = Literal["waitlist", "activation", "live", "ended"]

_CACHE_TTL_S = 30
_CACHE: dict = {"row": None, "fetched_at": 0.0}


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


def invalidate() -> None:
    _CACHE["row"] = None
    _CACHE["fetched_at"] = 0.0


def _row() -> dict:
    """The effective settings row (``DEFAULTS`` ⊕ the stored row), through
    the 30s cache. A DynamoDB read failure logs a warning and falls back to
    whatever is cached (or DEFAULTS on a cold cache) instead of raising —
    this is on the container-status poll path, and a DynamoDB hiccup must
    never 500 the fleet."""
    now = time.monotonic()
    if _CACHE["row"] is not None and now - _CACHE["fetched_at"] < _CACHE_TTL_S:
        return _CACHE["row"]
    try:
        stored = launch_settings_repo.get_sync()
        row = {**launch_settings_repo.DEFAULTS, **(stored or {})}
    except Exception:  # noqa: BLE001 — the poll path must never fail because DynamoDB hiccuped
        logger.warning("launch_phase: settings read failed; serving cached/default row", exc_info=True)
        row = _CACHE["row"] if _CACHE["row"] is not None else dict(launch_settings_repo.DEFAULTS)
    _CACHE["row"] = row
    _CACHE["fetched_at"] = now
    return row


def constants() -> dict:
    row = _row()
    return {
        "comp_slots": row["comp_slots"],
        "competition_days": row["competition_days"],
        "pot_front_cents": row["pot_front_cents"],
        "pot_per_entrant_cents": row["pot_per_entrant_cents"],
    }


def coupon_id() -> str:
    return _row()["stripe_coupon_id"]


def schedule() -> Schedule | None:
    row = _row()
    if not row["enabled"] or not row["drop_at"] or not row["competition_start_at"]:
        return None
    drop = parse_iso(row["drop_at"])
    start = parse_iso(row["competition_start_at"])
    if start <= drop:
        # Defense in depth — launch_settings.validate() already refuses this
        # order at write time, so this only fires against a row that bypassed
        # validation (a hand-edited DynamoDB item).
        raise ValueError("launch settings: competition_start_at must be after drop_at")
    return Schedule(
        drop_at=drop,
        competition_start_at=start,
        competition_end_at=start + timedelta(days=row["competition_days"]),
        comp_slots=row["comp_slots"],
        activation_window_hours=int((start - drop).total_seconds() // 3600),
        competition_days=row["competition_days"],
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
