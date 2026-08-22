"""In-process per-key sliding-window bucket — burst smoothing ONLY.
Per-process and several backend tasks run behind the ALB, so this is never a
real quota; durable fences (daily caps in DynamoDB) are. Each call surface
keeps its OWN bucket instance so one surface's flood cannot lock out another."""

import time
from collections import defaultdict, deque


class RateBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._buckets[key]
        while q and q[0] < now - 60.0:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._buckets.clear()
