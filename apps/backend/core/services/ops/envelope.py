"""Ops DTO envelope.

Every ops read returns {data, freshness_ts, source_state, errors}. A failed,
throttled, or timed-out read surfaces as source_state != "ok" with the failing
domain in errors[] — NEVER as empty data that renders 'all clear'. This is the
contract that prevents silent false-negatives on the operator console.
"""

from __future__ import annotations

import logging
from typing import Any

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
