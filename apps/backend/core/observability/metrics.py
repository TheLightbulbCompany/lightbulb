"""CloudWatch Embedded Metric Format (EMF) emitter.

Writes metrics as JSON log lines containing an `_aws.CloudWatchMetrics`
envelope. CloudWatch automatically extracts these into queryable metrics
from the existing ECS log stream — no extra IAM, no PutMetricData calls,
no additional cost beyond the log ingestion we're already paying for.

EMF spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html

Usage:
    from core.observability.metrics import put_metric, timing, gauge

    put_metric("container.provision", dimensions={"status": "ok"})

    with timing("container.lifecycle.latency", {"op": "start"}):
        await ecs.start_task(...)

    gauge("gateway.connection.open", len(pool))
"""

import json
import sys
import time
from contextlib import contextmanager
from typing import Iterator

from core.config import settings

NAMESPACE = "Isol8"

# Dimensions that must NOT appear in metrics (high cardinality = cost explosion).
# These belong in structured log fields instead.
#   - admin_user_id: unbounded per admin identity (admin_api.* middleware).
#   - tool_id: user-supplied/unbounded (key_service orphan-delete metric).
_DENIED_DIMENSIONS = frozenset({"user_id", "container_id", "request_id", "owner_id", "admin_user_id", "tool_id"})


def _get_env() -> str:
    return (settings.ENVIRONMENT or "dev").lower()


def put_metric(
    name: str,
    value: float = 1.0,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit one CloudWatch metric via EMF.

    Automatically injects `env` and `service` dimensions on every metric.
    Raises ValueError if a denied high-cardinality dimension is passed —
    this catches programming errors during development.
    """
    dims = dimensions or {}

    bad_keys = _DENIED_DIMENSIONS & set(dims.keys())
    if bad_keys:
        raise ValueError(
            f"high-cardinality dimension(s) {bad_keys} must not be metric dimensions; use structured log fields instead"
        )

    all_dims = {"env": _get_env(), "service": "isol8-backend", **dims}
    dim_keys = list(all_dims.keys())

    emf_line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [dim_keys],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **all_dims,
    }
    print(json.dumps(emf_line), file=sys.stdout, flush=True)


@contextmanager
def timing(name: str, dimensions: dict[str, str] | None = None) -> Iterator[None]:
    """Context manager that emits a latency metric in milliseconds.

    Works with both sync and async code inside the block:
        with timing("chat.e2e.latency"):
            await some_async_call()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        put_metric(name, value=elapsed_ms, unit="Milliseconds", dimensions=dimensions)


def gauge(name: str, value: float, dimensions: dict[str, str] | None = None) -> None:
    """Emit a point-in-time gauge value (e.g., connection pool size)."""
    put_metric(name, value=value, unit="Count", dimensions=dimensions)
