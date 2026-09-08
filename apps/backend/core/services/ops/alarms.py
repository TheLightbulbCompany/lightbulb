"""CloudWatch alarms read for the operator attention strip."""

from __future__ import annotations

import asyncio
import re
import urllib.parse

import boto3

from core.config import settings

# Alarm-name convention from the observability stack (every alarmName there is
# built from resourcePrefix() in apps/infra/lib/table-names.ts, which is
# `lightbulb-{env}-`):
#   lightbulb-<env>-P<n>-… = paging/critical, lightbulb-<env>-W<n>-… = warning.
_SEVERITY_RE = re.compile(r"^lightbulb-[^-]+-(P|W)\d+-")

_client = None


def _cw():
    global _client
    if _client is None:
        _client = boto3.client("cloudwatch", region_name=settings.AWS_REGION)
    return _client


def _severity(name: str | None) -> str:
    m = _SEVERITY_RE.match(name or "")
    if not m:
        return "unknown"
    return "critical" if m.group(1) == "P" else "warning"


def _console_url(name: str | None) -> str | None:
    """CloudWatch console deep link to the alarm detail page."""
    if not name:
        return None
    region = settings.AWS_REGION
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#alarmsV2:alarm/{urllib.parse.quote(name, safe='')}"
    )


def _row(a: dict, *, metric: str | None) -> dict:
    name = a.get("AlarmName")
    return {
        "name": name,
        "metric": metric,
        # `or None` so an empty-string description JSON-encodes as null and the
        # frontend's ?? fallback to the raw name still fires.
        "description": a.get("AlarmDescription") or None,
        "state_reason": a.get("StateReason") or None,
        "severity": _severity(name),
        "console_url": _console_url(name),
    }


async def alarms_in_alarm() -> list[dict]:
    """Alarms currently in ALARM state (<=100/page, paginated).

    Each row: {name, metric, description, state_reason, severity, console_url}.
    """
    out: list[dict] = []
    token = None
    for _ in range(5):  # page cap
        kwargs = {"StateValue": "ALARM", "AlarmTypes": ["MetricAlarm", "CompositeAlarm"], "MaxRecords": 100}
        if token:
            kwargs["NextToken"] = token
        resp = await asyncio.to_thread(_cw().describe_alarms, **kwargs)
        for a in resp.get("MetricAlarms", []):
            out.append(_row(a, metric=a.get("MetricName")))
        for a in resp.get("CompositeAlarms", []):
            out.append(_row(a, metric=None))
        token = resp.get("NextToken")
        if not token:
            break
    return out
