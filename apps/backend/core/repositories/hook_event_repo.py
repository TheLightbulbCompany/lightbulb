"""Repository for the hook-events audit table.

Stores one row per webhook call routed through ``routers/hooks_proxy.py``.
TTL on ``expires_at`` (Unix seconds) keeps the table self-trimming at
30 days. Writes are best-effort — a DDB failure must not drop the
proxied call. Reads are added in the follow-up control-panel spec.
"""

import logging
import time
from typing import Any

from ulid import ULID

from core.dynamodb import get_table

logger = logging.getLogger(__name__)

_USER_AGENT_MAX = 512
_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _get_table():
    return get_table("hook-events")


def write_event(
    *,
    owner_id: str,
    hook_path: str,
    status_code: int,
    source_ip: str,
    user_agent: str,
    request_size: int,
    response_size: int,
) -> None:
    """Persist one audit row. Failures are logged and swallowed."""
    now_ms = int(time.time() * 1000)
    item: dict[str, Any] = {
        "owner_id": owner_id,
        "event_id": str(ULID()),
        "timestamp": now_ms,
        "hook_path": hook_path,
        "status_code": status_code,
        "source_ip": source_ip,
        "user_agent": user_agent[:_USER_AGENT_MAX],
        "request_size": request_size,
        "response_size": response_size,
        "expires_at": (now_ms // 1000) + _TTL_SECONDS,
    }
    try:
        _get_table().put_item(Item=item)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.warning("hook-events write failed owner=%s err=%s", owner_id, exc)
