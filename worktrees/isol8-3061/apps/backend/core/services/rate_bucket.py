"""In-process per-key sliding-window bucket — burst smoothing ONLY.
Per-process and several backend tasks run behind the ALB, so this is never a
real quota; durable fences (daily caps in DynamoDB) are. Each call surface
keeps its OWN bucket instance so one surface's flood cannot lock out another."""

import time
from collections import defaultdict, deque

_SWEEP_EVERY = 1024


class RateBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._calls = 0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._buckets[key]
        while q and q[0] < now - 60.0:
            q.popleft()
        allowed = len(q) < self.per_minute
        if allowed:
            q.append(now)
        self._calls += 1
        if self._calls % _SWEEP_EVERY == 0:
            self._sweep(now)
        return allowed

    def _sweep(self, now: float) -> None:
        """Evict keys idle for >60s. Called periodically from allow() so
        owners who hit a hook-token route once don't stay in memory forever."""
        for k in [k for k, q in self._buckets.items() if not q or q[-1] < now - 60.0]:
            del self._buckets[k]

    def clear(self) -> None:
        self._buckets.clear()
