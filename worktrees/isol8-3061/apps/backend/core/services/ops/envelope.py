"""Ops DTO envelope + a fail-safe source-read wrapper.

Every ops read returns {data, freshness_ts, source_state, errors}. A failed,
throttled, or timed-out read surfaces as source_state != "ok" with the failing
domain in errors[] — NEVER as empty data that renders 'all clear'. This is the
contract that prevents silent false-negatives on the operator console.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from core.observability.metrics import put_metric
from core.dynamodb import utc_now_iso

logger = logging.getLogger(__name__)

# Semantic states — kept as plain strings for JSON transport.
OK = "ok"
DEGRADED = "degraded"
STALE = "stale"
UNAVAILABLE = "unavailable"
PARTIAL = "partial"


def envelope(
    data: Any,
    *,
    source_state: str = OK,
    errors: list[dict] | None = None,
    freshness_ts: str | None = None,
) -> dict:
    return {
        "data": data,
        "freshness_ts": freshness_ts or utc_now_iso(),
        "source_state": source_state,
        "errors": errors or [],
    }


async def read_source(
    domain: str,
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    timeout_s: float = 8.0,
) -> tuple[Any, str, list[dict]]:
    """Run one source read with a timeout; never raise.

    Returns (data, source_state, errors). On any failure returns
    (None, "unavailable", [{domain, reason}]) and emits an EMF error metric.
    """
    try:
        data = await asyncio.wait_for(coro_factory(), timeout=timeout_s)
        return data, OK, []
    except asyncio.TimeoutError:
        put_metric("ops.source.error", dimensions={"domain": domain, "reason": "timeout"})
        logger.warning("ops source %s timed out after %.1fs", domain, timeout_s)
        return None, UNAVAILABLE, [{"domain": domain, "reason": f"timeout after {timeout_s}s"}]
    except Exception as e:  # noqa: BLE001 — fail-safe by contract
        put_metric("ops.source.error", dimensions={"domain": domain, "reason": "exception"})
        logger.warning("ops source %s failed: %s", domain, e)
        return None, UNAVAILABLE, [{"domain": domain, "reason": str(e)}]
