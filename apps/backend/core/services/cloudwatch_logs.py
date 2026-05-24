"""CloudWatch Logs reader for the admin dashboard.

Two callers in v1:
- /admin/users/{user_id}/logs — per-user inline log viewer (filter_user_logs).
- /admin/health — fleet-scoped recent errors panel (recent_errors_fleet).

Backed by boto3 logs.FilterLogEvents on the backend service log group.
The IAM permission lives on the backend ECS task role
(see apps/infra/lib/stacks/service-stack.ts: CloudWatchLogsReadForAdmin).

Log group naming matches service-stack.ts:519 + isol8-stage.ts:113 +
local-stage.ts:116 — `/ecs/isol8-${env}` (no /aws prefix, no -backend
suffix). Keep this module's IAM scope in sync with the resolved log
group ARN.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("logs", region_name=settings.AWS_REGION)
    return _client


def _backend_log_group() -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"/ecs/isol8-{env}"


def _parse_event(raw: dict) -> dict:
    """Structured-log JSON → row shape consumed by the admin Logs tab."""
    parsed = None
    try:
        parsed = json.loads(raw["message"])
    except (ValueError, TypeError):
        pass

    iso_ts = datetime.fromtimestamp(raw["timestamp"] / 1000, tz=timezone.utc).isoformat()

    if parsed is not None:
        return {
            "timestamp": iso_ts,
            "level": parsed.get("level"),
            "message": parsed.get("message", raw["message"]),
            "correlation_id": parsed.get("correlation_id"),
            "raw_json": parsed,
        }
    return {
        "timestamp": iso_ts,
        "level": None,
        "message": raw["message"],
        "correlation_id": None,
        "raw_json": None,
    }


async def filter_user_logs(
    *,
    user_id: str,
    level: str = "ERROR",
    hours: int = 24,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    """Return recent structured logs filtered to a specific user.

    Returns: {events: list[dict], cursor: str | None, missing: bool}
    - cursor is the FilterLogEvents nextToken — opaque to callers, pass
      back as `cursor` to fetch the next page (CEO E4).
    - missing=True when the log group does not exist (LocalStack /
      fresh env). Callers should render "no logs configured" rather than
      treating it as an empty result.
    - On non-NotFound errors, returns {events: [], missing: False, error: str}
      so the admin page degrades gracefully instead of 500ing.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    kwargs: dict[str, Any] = {
        "logGroupName": _backend_log_group(),
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "filterPattern": f'{{ $.user_id = "{user_id}" && $.level = "{level}" }}',
        "limit": min(limit, 100),
    }
    if cursor:
        kwargs["nextToken"] = cursor

    client = _get_client()
    try:
        response = await run_in_thread(client.filter_log_events, **kwargs)
    except client.exceptions.ResourceNotFoundException:
        return {"events": [], "cursor": None, "missing": True}
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        logger.warning("cloudwatch_logs.filter_user_logs failed: %s", e)
        return {"events": [], "cursor": None, "missing": False, "error": str(e)}

    return {
        "events": [_parse_event(raw) for raw in response.get("events", [])],
        "cursor": response.get("nextToken"),
        "missing": False,
    }


async def recent_errors_fleet(*, hours: int = 24, limit: int = 20) -> list[dict]:
    """Cross-user recent errors for /admin/health.

    Same primitive as filter_user_logs but filter pattern is level-only.
    Returns simplified row shape: {timestamp, user_id, message, correlation_id}.
    Empty list on log-group-not-found or any error (the health endpoint
    must never 500 because of CWL).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    client = _get_client()
    try:
        response = await run_in_thread(
            client.filter_log_events,
            logGroupName=_backend_log_group(),
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            filterPattern='{ $.level = "ERROR" }',
            limit=min(limit, 100),
        )
    except client.exceptions.ResourceNotFoundException:
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("cloudwatch_logs.recent_errors_fleet failed: %s", e)
        return []

    out = []
    for raw in response.get("events", []):
        try:
            parsed = json.loads(raw["message"])
        except (ValueError, TypeError):
            parsed = {}
        out.append(
            {
                "timestamp": datetime.fromtimestamp(raw["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                "user_id": parsed.get("user_id"),
                "message": parsed.get("message") or raw["message"],
                "correlation_id": parsed.get("correlation_id"),
            }
        )
    return out
