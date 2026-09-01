"""Launch Competition schedule — reads the launch-settings DynamoDB row
through a 30-second in-process cache.

The row (``core.repositories.launch_settings_repo``, edited via
``core.services.launch_settings`` from the admin console) is THE source of
the dates and pot constants for every surface. A DynamoDB read failure falls
back to the cached/default row (``_row()``), and a row that fails to derive a
schedule (bad types, bad dates) is treated as feature-off rather than raised
(``schedule()``). ``enabled=False`` or any date missing = feature off.

Phase ladder: open (sheet accepting joins) → closed (the admin close action
stamped ``closed_at``, or ``sheet_closes_at`` passed) → competition → ended.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

from core.repositories import launch_settings_repo

logger = logging.getLogger(__name__)

Phase = Literal["open", "closed", "competition", "ended"]

_CACHE_TTL_S = 30
_CACHE: dict = {"row": None, "fetched_at": 0.0}


@dataclass(frozen=True)
class Schedule:
    sheet_closes_at: datetime
    competition_start_at: datetime
    competition_end_at: datetime
    comp_slots: int
    closed_at: datetime | None


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
    whatever is cached (or DEFAULTS on a cold cache) instead of raising — a
    DynamoDB hiccup must never 500 a public surface. A cold-cache failure is
    deliberately NOT cached: the next call retries."""
    now = time.monotonic()
    if _CACHE["row"] is not None and now - _CACHE["fetched_at"] < _CACHE_TTL_S:
        return _CACHE["row"]
    try:
        stored = launch_settings_repo.get_sync()
        row = {**launch_settings_repo.DEFAULTS, **(stored or {})}
    except Exception:  # noqa: BLE001 — serving surfaces must never fail because DynamoDB hiccuped
        logger.warning("launch_phase: settings read failed; serving cached/default row", exc_info=True)
        if _CACHE["row"] is None:
            return dict(launch_settings_repo.DEFAULTS)
        row = _CACHE["row"]
    _CACHE["row"] = row
    _CACHE["fetched_at"] = now
    return row


def constants() -> dict:
    row = _row()
    return {
        "comp_slots": row["comp_slots"],
        "pot_front_cents": row["pot_front_cents"],
        "pot_per_entrant_cents": row["pot_per_entrant_cents"],
        "pot_per_seat_cents": row["pot_per_seat_cents"],
        "referral_jump": row["referral_jump"],
    }


def coupon_id() -> str:
    return _row()["stripe_coupon_id"]


def email_copy() -> dict:
    """The admin's per-variant email copy OVERRIDES (see
    ``core.services.launch_sheet_email``). Rides the same 30s row cache, so
    neither the join path nor a 200-send close pays a read per email."""
    return _row().get("email_copy") or {}


def schedule() -> Schedule | None:
    row = _row()
    if (
        not row["enabled"]
        or not row["sheet_closes_at"]
        or not row["competition_start_at"]
        or not row["competition_end_at"]
    ):
        return None
    try:
        closes = parse_iso(row["sheet_closes_at"])
        start = parse_iso(row["competition_start_at"])
        end = parse_iso(row["competition_end_at"])
        if start <= closes or end <= start:
            # launch_settings.validate() already refuses these orderings at
            # write time, so this only fires against a row that bypassed
            # validation (a hand-edited DynamoDB item) — log and go
            # feature-off, not a 500.
            raise ValueError("dates out of order")
        closed_at = parse_iso(row["closed_at"]) if row.get("closed_at") else None
        return Schedule(
            sheet_closes_at=closes,
            competition_start_at=start,
            competition_end_at=end,
            comp_slots=row["comp_slots"],
            closed_at=closed_at,
        )
    except Exception:  # noqa: BLE001 — a malformed row must not 500 any surface
        logger.warning("launch_phase: settings row failed to derive a schedule; treating as feature off", exc_info=True)
        return None


def phase(s: Schedule, now: datetime | None = None) -> Phase:
    now = now or datetime.now(timezone.utc)
    if s.closed_at is None and now < s.sheet_closes_at:
        return "open"
    if now < s.competition_start_at:
        return "closed"
    if now < s.competition_end_at:
        return "competition"
    return "ended"


def pot_cents(entrants: int, team_seats: Iterable[int] = ()) -> int:
    """front + per-entrant × individual roster + one (per-entrant + per-seat ×
    seats) block per COUNTED team entry.

    ``team_seats`` is the seat count of each team the founders have marked
    counted (``launch_teams_repo``) — an unreviewed submission passes nothing
    here, so it can never inflate the pot. A team is one entrant with a size:
    it contributes the same per-entrant amount an individual does, plus the
    per-seat amount for each seat."""
    c = constants()
    total = c["pot_front_cents"] + c["pot_per_entrant_cents"] * max(entrants, 0)
    for seats in team_seats:
        total += c["pot_per_entrant_cents"] + c["pot_per_seat_cents"] * max(int(seats), 0)
    return total
