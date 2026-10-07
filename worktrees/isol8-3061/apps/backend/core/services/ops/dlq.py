"""Work-queue DLQ depth from CloudWatch — the messaging reliability counter.

``openclaw-work-dlq.fifo`` (apps/infra/lib/stacks/api-stack.ts) collects every
room wake the worker could not deliver, so its depth is the count of agent
replies that will never arrive without a redrive. Sibling reader to
``gate_blocked``; same client, same GetMetricData shape, same
non-``Complete`` → raise rule (a Forbidden read must not surface as a calm zero).

SQS publishes ``ApproximateNumberOfMessagesVisible`` on a 5-minute period, so
this is the newest datapoint in the last hour, not an instant depth.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

_PERIOD_S = 300  # SQS's own publication period
_LOOKBACK_S = 3600


def queue_name() -> str:
    """The DLQ's name, derived from the deployed work-queue URL.

    ``OPENCLAW_WORK_QUEUE_URL`` is what CDK actually wired into the task
    (service-stack.ts), and both queues are created together in api-stack.ts as
    ``<prefix>openclaw-work.fifo`` / ``<prefix>openclaw-work-dlq.fifo``. Deriving
    from it means a renamed or re-prefixed queue cannot leave this reader
    silently pointed at a name nothing publishes. The literal is the fallback for
    a local/unset environment only.
    """
    url = (settings.OPENCLAW_WORK_QUEUE_URL or "").rstrip("/")
    name = url.rsplit("/", 1)[-1] if url else ""
    if name.endswith(".fifo"):
        return f"{name[: -len('.fifo')]}-dlq.fifo"
    return f"lightbulb-{(settings.ENVIRONMENT or 'dev').lower()}-openclaw-work-dlq.fifo"


async def work_dlq_depth() -> int:
    end = datetime.now(timezone.utc)
    resp = await run_in_thread(
        boto3.client("cloudwatch", region_name=settings.AWS_REGION).get_metric_data,
        MetricDataQueries=[
            {
                "Id": "dlq",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SQS",
                        "MetricName": "ApproximateNumberOfMessagesVisible",
                        "Dimensions": [{"Name": "QueueName", "Value": queue_name()}],
                    },
                    "Period": _PERIOD_S,
                    "Stat": "Maximum",
                },
            }
        ],
        StartTime=end - timedelta(seconds=_LOOKBACK_S),
        EndTime=end,
        ScanBy="TimestampDescending",
    )
    result = (resp.get("MetricDataResults") or [{}])[0]
    if result.get("StatusCode") != "Complete":
        raise RuntimeError(f"GetMetricData dlq: {result.get('StatusCode')} {result.get('Messages')}")
    values = result.get("Values") or []
    return int(values[0]) if values else 0
