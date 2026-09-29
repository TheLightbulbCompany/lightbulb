"""/admin/health aggregator.

Composes:
- Container fleet counts grouped by status (from the containers index via ops.fleet).
- Upstream probes for Clerk, Stripe, DDB — each 2s timeout, results
  cached for 30s (CEO P2) so the health page doesn't block on slow
  upstreams every render.
- Background-task state for the async tasks spawned in main.py lifespan
  (scheduled_worker, trial_idle_sweeper, running_gauges, ...).
  main.py registers those task references in the BACKGROUND_TASKS dict at
  startup; we read their .done()/.exception() state here.
- Recent fleet-wide errors via cloudwatch_logs.recent_errors_fleet.

Read by routers/admin.py: GET /admin/system/health.
"""

import asyncio
import logging
import time
from typing import Any

import boto3
import httpx

from core.config import settings
from core.services import cloudwatch_logs
from core.services.ops import fleet as fleet_ops

logger = logging.getLogger(__name__)


# Probe cache: {value: dict, ts: monotonic_seconds}
_PROBE_CACHE_TTL_S = 30
_probe_cache: dict[str, Any] = {"ts": 0.0, "value": None}

# Set by main.py lifespan to {task_name: asyncio.Task}.
# Read here to surface "is the trial_idle_sweeper still running" on /admin/health.
BACKGROUND_TASKS: dict[str, asyncio.Task] = {}


async def _probe_clerk() -> dict:
    """Fetch the Clerk JWKS — cheap, exercises auth dependency."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.CLERK_ISSUER}/.well-known/jwks.json")
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": "ok" if response.status_code == 200 else "degraded",
            "latency_ms": latency_ms,
            "http_status": response.status_code,
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "error": str(e)}


async def _probe_stripe() -> dict:
    """Lightweight Stripe ping — Account.retrieve verifies key + connectivity."""
    if not settings.STRIPE_SECRET_KEY:
        return {"status": "unconfigured"}
    started = time.monotonic()
    try:
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        await asyncio.to_thread(stripe.Account.retrieve)
        return {"status": "ok", "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"status": "degraded", "error": str(e)}


async def _probe_ddb() -> dict:
    """DDB ping via DescribeTable on the admin-actions table."""
    started = time.monotonic()
    try:
        client = boto3.client("dynamodb", region_name=settings.AWS_REGION)
        table_name = f"{settings.DYNAMODB_TABLE_PREFIX}admin-actions"
        await asyncio.to_thread(client.describe_table, TableName=table_name)
        return {"status": "ok", "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "error": str(e)}


async def _all_probes() -> dict:
    """Run all upstream probes in parallel; cache for _PROBE_CACHE_TTL_S."""
    if _probe_cache["value"] is not None and (time.monotonic() - _probe_cache["ts"]) < _PROBE_CACHE_TTL_S:
        return _probe_cache["value"]

    results = await asyncio.gather(
        _probe_clerk(),
        _probe_stripe(),
        _probe_ddb(),
        return_exceptions=True,
    )

    def _safe(r: Any) -> dict:
        if isinstance(r, BaseException):
            return {"status": "down", "error": str(r)}
        return r

    value = {
        "clerk": _safe(results[0]),
        "stripe": _safe(results[1]),
        "ddb": _safe(results[2]),
    }
    _probe_cache["ts"] = time.monotonic()
    _probe_cache["value"] = value
    return value


async def _fleet_counts() -> dict[str, int]:
    """Workspaces by status, from the containers index.

    Delegates to fleet_ops.fleet_counts_live() under a 2s latency bound so a hung
    scan cannot block GET /admin/system/health; get_system_health()'s
    _safe wrapper degrades a timeout (or any error) to {}, so the admin health
    page always renders.
    """
    return await asyncio.wait_for(fleet_ops.fleet_counts_live(), timeout=2.0)


def _background_tasks_status() -> dict[str, dict]:
    """Snapshot current state of registered async tasks."""
    out: dict[str, dict] = {}
    for name, task in BACKGROUND_TASKS.items():
        if task is None:
            out[name] = {"status": "unregistered"}
            continue
        if task.done():
            try:
                exc = task.exception()
                out[name] = {"status": "stopped", "error": str(exc) if exc else None}
            except asyncio.CancelledError:
                out[name] = {"status": "cancelled"}
            except asyncio.InvalidStateError:
                out[name] = {"status": "stopped"}
        else:
            out[name] = {"status": "running"}
    return out


async def get_system_health() -> dict:
    """Top-level entry — composes everything for /admin/system/health.

    Never raises — all upstream errors degrade to {status: "down"} on
    that probe so the admin health page always renders.
    """
    upstreams, fleet, recent_errors = await asyncio.gather(
        _all_probes(),
        _fleet_counts(),
        cloudwatch_logs.recent_errors_fleet(hours=24, limit=10),
        return_exceptions=True,
    )

    def _safe(value: Any, fallback: Any) -> Any:
        if isinstance(value, BaseException):
            logger.warning("system_health component failed: %s", value)
            return fallback
        return value

    return {
        "upstreams": _safe(upstreams, {}),
        "fleet": _safe(fleet, {}),
        "background_tasks": _background_tasks_status(),
        "messaging": {
            "appsync_publisher": {
                "status": "configured" if settings.APPSYNC_EVENTS_HTTP_ENDPOINT.strip() else "unconfigured"
            },
            "openclaw_work_queue": {
                "status": "configured" if settings.OPENCLAW_WORK_QUEUE_URL.strip() else "unconfigured"
            },
        },
        "recent_errors": _safe(recent_errors, []),
    }
