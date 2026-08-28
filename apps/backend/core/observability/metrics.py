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

NAMESPACE = "LightbulbAI"

# Dimensions that must NOT appear in metrics (high cardinality = cost explosion).
# These belong in structured log fields instead.
#   - admin_user_id: unbounded per admin identity (admin_api.* middleware).
#   - tool_id: user-supplied/unbounded (key_service orphan-delete metric).
_DENIED_DIMENSIONS = frozenset({"user_id", "container_id", "request_id", "owner_id", "admin_user_id", "tool_id"})

# The fleet-wide rollup dimension set. CloudWatch treats EVERY dimension set as
# its own metric identity, so a metric emitted only as [env, service, reason]
# is INVISIBLE to a query for [env, service] — it is a different metric, not a
# filtered view of the same one. Every alarm and dashboard widget in
# observability-stack.ts binds { env, service } by default (createAlarm injects
# exactly those two), so emitting the full set alone left those alarms watching
# an identity nothing published: W2/W8/W10 could never fire. Emitting both sets
# from the one record fixes that for every metric at once, present and future.
#
# The one trap this creates: NEVER emit the same metric twice for one event,
# once dimensionless and once with dimensions. Both land on the rollup and the
# event is counted twice. `provision.gate.blocked` did exactly that (to feed
# W64 before the rollup existed) and the dimensionless call was deleted.
_ROLLUP_KEYS = ["env", "service"]

# The status rollup: [env, service, status], emitted whenever a metric carries a
# `status` dimension.
#
# Why a THIRD set rather than a second. Several alarms need "how many errors,
# across every value of the qualifier" — EFS access-point failures across all
# `op`s, channel failures across all `provider`s. The full set is too narrow
# (one identity per op/provider) and the [env, service] rollup is too wide (it
# folds ok and error together, so an error alarm would fire on success traffic).
# Neither published identity answers the question.
#
# The alternative was a `SUM(SEARCH(...))` metric-math expression in the alarm,
# which is what observability-stack.ts originally did. CloudWatch rejects it
# outright — "SEARCH is not supported on Metric Alarms" — and the stack rolled
# back on every deploy. That is a platform limitation with no workaround at the
# alarm layer, so the aggregate has to exist as a real published identity, which
# is what this set is.
#
# Same no-double-count rule as _ROLLUP_KEYS: these are distinct dimension sets
# within ONE record, so CloudWatch aggregates each independently from a single
# value. Emitting the same event twice in two calls still double-counts.
_STATUS_ROLLUP_KEYS = [*_ROLLUP_KEYS, "status"]


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

    all_dims = {"env": _get_env(), "service": "lightbulb-backend", **dims}
    dim_keys = list(all_dims.keys())

    # Full set first (what an operator slices by: chat.error by reason,
    # container.lifecycle.latency by op), then the status rollup (see
    # _STATUS_ROLLUP_KEYS), then the fleet rollup (see _ROLLUP_KEYS). Each is
    # skipped when it would duplicate the full set — an identical set repeated
    # in one record double-counts.
    dimension_sets = [dim_keys]
    if "status" in dims and dim_keys != _STATUS_ROLLUP_KEYS:
        dimension_sets.append(_STATUS_ROLLUP_KEYS)
    if dim_keys != _ROLLUP_KEYS:
        dimension_sets.append(_ROLLUP_KEYS)

    emf_line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": dimension_sets,
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
