"""``provision.gate.blocked`` by code over the last 7 days, from CloudWatch.

The gate emits the metric via EMF only (core/services/provision_gate.py:411,
``put_metric("provision.gate.blocked", dimensions={"code": ...})``); nothing
persists the code to DynamoDB, so CloudWatch is the only reader. The
emitter publishes the FULL dimension set ``[env, service, code]``
(core/observability/metrics.py), which is what GetMetricData must match
exactly. One MetricDataQuery per known code (≤500 per call).

ponytail: explicit code list — switch to a SEARCH() expression if codes
multiply; today there are 11 and they are literals in provision_gate.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

GATE_CODES: tuple[str, ...] = (
    "byo_provider_required",
    "credits_required",
    "invitation_pending",
    "oauth_required",
    "payment_past_due",
    "provider_required",
    "signup_required",
    "subscription_required",
    "team_billing_required",
    "trial_daily_cap",
    "trial_expired",
)
_PERIOD_S = 7 * 24 * 3600


def _client():
    return boto3.client("cloudwatch", region_name=settings.AWS_REGION)


async def last_7d() -> list[dict]:
    env_name = (settings.ENVIRONMENT or "dev").lower()  # mirrors metrics._get_env
    end = datetime.now(timezone.utc)
    queries = [
        {
            "Id": f"c_{code}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "Isol8",
                    "MetricName": "provision.gate.blocked",
                    "Dimensions": [
                        {"Name": "env", "Value": env_name},
                        {"Name": "service", "Value": "isol8-backend"},
                        {"Name": "code", "Value": code},
                    ],
                },
                "Period": _PERIOD_S,
                "Stat": "Sum",
            },
        }
        for code in GATE_CODES
    ]
    resp = await run_in_thread(
        _client().get_metric_data,
        MetricDataQueries=queries,
        StartTime=end - timedelta(seconds=_PERIOD_S),
        EndTime=end,
    )
    counts = {r["Id"][2:]: int(sum(r.get("Values") or [])) for r in resp.get("MetricDataResults", [])}
    return sorted(
        ({"code": c, "n": n} for c, n in counts.items() if n > 0),
        key=lambda x: (-x["n"], x["code"]),
    )
