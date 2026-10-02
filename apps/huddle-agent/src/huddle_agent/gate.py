"""When may an agent announce its finished work unprompted (D §8)? No network, no LiveKit."""

from __future__ import annotations

import os
from collections import deque

ANNOUNCE_QUIET = 3  # ponytail: inferred (D §8); seconds the whole room is quiet before an agent speaks up
MIN_GAP = 90  # proactive announcements per room
PER_HOUR = 10


def enabled() -> bool:
    """The kill switch: LIGHTBULB_PROACTIVE=off leaves only a tap on a has_news tile."""
    return os.environ.get("LIGHTBULB_PROACTIVE", "").strip().lower() != "off"


class RateLimit:
    def __init__(self, gap: float = MIN_GAP, per_hour: int = PER_HOUR):
        self._gap, self._per_hour = gap, per_hour
        self._at: deque[float] = deque()

    def ok(self, now: float) -> bool:
        while self._at and now - self._at[0] >= 3600:
            self._at.popleft()
        return len(self._at) < self._per_hour and (not self._at or now - self._at[-1] >= self._gap)

    def record(self, now: float) -> None:
        self._at.append(now)
