"""CloudWatch alarms read for the operator attention strip."""

from __future__ import annotations

import asyncio

import boto3

from core.config import settings

_client = None


def _cw():
    global _client
    if _client is None:
        _client = boto3.client("cloudwatch", region_name=settings.AWS_REGION)
    return _client


async def alarms_in_alarm() -> list[dict]:
    """Alarms currently in ALARM state: [{name, metric}] (<=100/page, paginated)."""
    out: list[dict] = []
    token = None
    for _ in range(5):  # page cap
        kwargs = {"StateValue": "ALARM", "AlarmTypes": ["MetricAlarm", "CompositeAlarm"], "MaxRecords": 100}
        if token:
            kwargs["NextToken"] = token
        resp = await asyncio.to_thread(_cw().describe_alarms, **kwargs)
        for a in resp.get("MetricAlarms", []):
            out.append({"name": a.get("AlarmName"), "metric": a.get("MetricName")})
        for a in resp.get("CompositeAlarms", []):
            out.append({"name": a.get("AlarmName"), "metric": None})
        token = resp.get("NextToken")
        if not token:
            break
    return out
