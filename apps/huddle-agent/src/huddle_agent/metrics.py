"""CloudWatch EMF metrics on stdout."""

from __future__ import annotations

import json
import sys
import time


def emit_metric(
    name: str, value: float, unit: str = "Seconds", dims: dict[str, str] | None = None
) -> None:
    """One CloudWatch EMF line on stdout (same shape as the backend's put_metric). `dims` are enums only:
    never transcript text."""
    dims = dims or {}
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "LightbulbAI",
                    "Dimensions": [list(dims)],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        **dims,
        name: value,
    }
    print(json.dumps(line), file=sys.stdout, flush=True)
