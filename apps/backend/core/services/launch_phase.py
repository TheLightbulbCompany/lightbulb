"""Launch Competition schedule — reads the launch-settings DynamoDB row
through a 30-second in-process cache.

The row (``core.repositories.launch_settings_repo``, edited via
``core.services.launch_settings`` from the admin console) is THE source of
the dates and pot constants for every surface. This module sits on the
container-status poll path (``core.services.launch_hold``), so it never lets
that path fail: a DynamoDB read failure falls back to the cached/default row
(``_row()``), and a row that fails to derive a schedule (bad types, bad
dates) is treated as feature-off rather than raised (``schedule()``).
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
_CACHE: dict = {"row": None, "fetched_at": 0.0, "unavailable": False}


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
    _CACHE["unavailable"] = False


def _row() -> dict:
    """The effective settings row (``DEFAULTS`` ⊕ the stored row), through
    the 30s cache. A DynamoDB read failure logs a warning and falls back to
    whatever is cached (or DEFAULTS on a cold cache) instead of raising —
    this is on the container-status poll path, and a DynamoDB hiccup must
    never 500 the fleet.

    A cold-cache failure is deliberately NOT cached: the next call retries
    (rather than serving a guess for 30 seconds), and ``settings_unavailable``
    reports that the DEFAULTS just served are a guess rather than the row."""
    now = time.monotonic()
    if _CACHE["row"] is not None and now - _CACHE["fetched_at"] < _CACHE_TTL_S:
        return _CACHE["row"]
    try:
        stored = launch_settings_repo.get_sync()
        row = {**launch_settings_repo.DEFAULTS, **(stored or {})}
    except Exception:  # noqa: BLE001 — the poll path must never fail because DynamoDB hiccuped
        logger.warning("launch_phase: settings read failed; serving cached/default row", exc_info=True)
        if _CACHE["row"] is None:
            _CACHE["unavailable"] = True
            return dict(launch_settings_repo.DEFAULTS)
        row = _CACHE["row"]
    _CACHE["unavailable"] = False
    _CACHE["row"] = row
    _CACHE["fetched_at"] = now
    return row


def settings_unavailable() -> bool:
    """True iff the last read failed with nothing cached to fall back to —
    the row every launch surface is reading right now is DEFAULTS-as-a-guess
    (feature off), not the stored row.

    Only ``launch_hold`` acts on this: un-holding an owner is irreversible
    (it stamps the 3-day trial clock), so a container-less owner is held
    rather than released on a DynamoDB blip. Every other reader stays
    fail-open — ``require_launch`` 404s, the webhook join is skipped."""
    return bool(_CACHE["unavailable"])


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
    try:
        drop = parse_iso(row["drop_at"])
        start = parse_iso(row["competition_start_at"])
        if start <= drop:
            # launch_settings.validate() already refuses this order at write
            # time, so this only fires against a row that bypassed validation
            # (a hand-edited DynamoDB item) — treated the same as any other
            # malformed row below: log and go feature-off, not a 500.
            raise ValueError("competition_start_at must be after drop_at")
        return Schedule(
            drop_at=drop,
            competition_start_at=start,
            competition_end_at=start + timedelta(days=row["competition_days"]),
            comp_slots=row["comp_slots"],
            activation_window_hours=int((start - drop).total_seconds() // 3600),
            competition_days=row["competition_days"],
        )
    except Exception:  # noqa: BLE001 — the poll path must never fail because a row is malformed
        logger.warning("launch_phase: settings row failed to derive a schedule; treating as feature off", exc_info=True)
        return None


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
