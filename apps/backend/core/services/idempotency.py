"""Per-endpoint idempotency decorator using an in-memory TTL cache.

Per CEO D1: admin write endpoints accept an optional
`Idempotency-Key` HTTP header. Two POSTs with the same key within
TTL_S return the cached response and short-circuit the underlying
service call. Defends against rapid double-clicks and two-admin races.

In-memory storage is fine for v1 — single backend instance, modest
admin volume. Move to a distributed cache (Redis/DDB) if/when admin
load demands.

The cache key is `(handler_name, idempotency_key)` so two distinct
endpoints with the same Idempotency-Key still execute independently.
"""

import functools
import time
from typing import Any, Callable

# {(handler_name, key): (expires_at_monotonic, response_payload)}
_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def _purge_expired(now: float) -> None:
    expired = [k for k, (exp, _) in _cache.items() if exp <= now]
    for k in expired:
        _cache.pop(k, None)


def reset_cache() -> None:
    """Test helper — clears the cache between tests."""
    _cache.clear()


def idempotency(*, header: str = "Idempotency-Key", ttl_s: int = 60) -> Callable:
    """Decorate a router handler to short-circuit on repeated Idempotency-Keys.

    The wrapped handler MUST take `request: Request` (the FastAPI Request
    object) as a kwarg. The decorator reads the header off it, looks up
    the cache, and either returns the cached response or runs the handler
    and caches the result.

    Requests without the header bypass the cache entirely (each call hits
    the underlying handler).
    """

    def decorator(handler: Callable) -> Callable:
        handler_id = f"{handler.__module__}.{handler.__qualname__}"

        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            key = None
            if request is not None and hasattr(request, "headers"):
                key = request.headers.get(header)

            if not key:
                return await handler(*args, **kwargs)

            now = time.monotonic()
            _purge_expired(now)
            cache_key = (handler_id, key)
            cached = _cache.get(cache_key)
            if cached is not None:
                _, payload = cached
                return payload

            result = await handler(*args, **kwargs)
            _cache[cache_key] = (now + ttl_s, result)
            return result

        return wrapped

    return decorator
