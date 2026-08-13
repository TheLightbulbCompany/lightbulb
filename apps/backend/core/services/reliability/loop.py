"""Policy and evidence assembly for the production reliability loop.

This module is intentionally side-effect free. CloudWatch/Sentry/Paperclip/GitHub
adapters can call it to classify a signal and produce the provenance payload
attached to a triage issue or reviewed PR artifact, while unit tests exercise the
core safety gates without touching production services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class SignalSource(str, Enum):
    CLOUDWATCH = "cloudwatch"
    SENTRY = "sentry"


@dataclass(frozen=True)
class ReliabilitySignal:
    """Normalized production/non-production alert signal.

    `fingerprint` is the deduplication key supplied by the source adapter
    (CloudWatch alarm ARN/name, Sentry issue fingerprint, etc.). Customer- and
    release-level identifiers are structured fields so they remain queryable in
    Paperclip/GitHub without leaking raw log payloads into metric dimensions.
    """

    source: SignalSource
    fingerprint: str
    title: str
    environment: str
    occurrences: int = 1
    affected_users: int = 0
    revenue_impact: bool = False
    contains_secret: bool = False
    release: str | None = None
    trace_id: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None


def classify_severity(signal: ReliabilitySignal) -> Severity:
    """Classify impact with conservative escalation and secret safety.

    Secrets in a captured signal are always SEV1 because the next action is
    containment and redaction before any artifact is shared. Revenue impact or a
    broad customer blast radius pages at SEV1. Repeated production failures with
    a smaller known blast radius land at SEV2; non-production and low-volume
    signals are triageable SEV3/SEV4.
    """

    if signal.contains_secret or signal.revenue_impact or signal.affected_users >= 25:
        return Severity.SEV1
    if signal.environment == "prod" and (signal.affected_users >= 3 or signal.occurrences >= 10):
        return Severity.SEV2
    if signal.environment == "prod":
        return Severity.SEV3
    return Severity.SEV4


def dedupe_key(signal: ReliabilitySignal) -> str:
    """Stable key used before creating a new Paperclip incident issue."""

    return f"reliability:{signal.source.value}:{signal.environment}:{signal.fingerprint}"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def build_evidence_bundle(
    signal: ReliabilitySignal,
    *,
    logs_url: str | None = None,
    trace_url: str | None = None,
    sentry_url: str | None = None,
    cloudwatch_alarm_url: str | None = None,
    reproduction: str | None = None,
    customer_impact: str | None = None,
    redactions: list[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create the provenance bundle attached to triage/review artifacts.

    The bundle stores pointers and summaries, not raw logs. If the source says a
    secret was present, callers must redact first and record the redaction note;
    the bundle makes that explicit for review.
    """

    severity = classify_severity(signal)
    return {
        "schema": "isol8.reliability.evidence.v1",
        "dedupe_key": dedupe_key(signal),
        "severity": severity.value,
        "source": signal.source.value,
        "title": signal.title,
        "environment": signal.environment,
        "fingerprint": signal.fingerprint,
        "release": signal.release,
        "trace_id": signal.trace_id,
        "first_seen": _iso(signal.first_seen),
        "last_seen": _iso(signal.last_seen),
        "impact": {
            "occurrences": signal.occurrences,
            "affected_users": signal.affected_users,
            "revenue_impact": signal.revenue_impact,
            "customer_summary": customer_impact,
        },
        "links": {
            "cloudwatch_logs": logs_url,
            "cloudwatch_alarm": cloudwatch_alarm_url,
            "sentry_issue": sentry_url,
            "trace": trace_url,
        },
        "reproduction": reproduction,
        "redactions": redactions
        or ([] if not signal.contains_secret else ["source reported possible secret; raw payload withheld"]),
        "run_id": run_id,
        "required_human_gates": ["merge", "deploy", "destructive_remediation", "sev1_or_secret_handling"],
        "next_state": "paperclip_issue_triage",
    }


def is_controlled_test_bundle(bundle: Mapping[str, Any]) -> bool:
    """Return true only for safe test signals allowed through automation.

    Controlled tests must be non-prod, explicit, and free of secret flags. This
    gate prevents a synthetic-path test from accidentally normalizing production
    destructive action or leaking raw incident material.
    """

    return (
        bundle.get("environment") in {"local", "test", "dev", "staging"}
        and str(bundle.get("fingerprint", "")).startswith("controlled-test:")
        and not bundle.get("redactions")
    )
