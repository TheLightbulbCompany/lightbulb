from datetime import datetime, timezone

from core.services.reliability.loop import (
    ReliabilitySignal,
    Severity,
    SignalSource,
    build_evidence_bundle,
    classify_severity,
    dedupe_key,
    is_controlled_test_bundle,
)


def test_controlled_dev_signal_builds_reviewable_provenance_bundle():
    signal = ReliabilitySignal(
        source=SignalSource.CLOUDWATCH,
        fingerprint="controlled-test:chat-error-rate",
        title="Controlled chat error-rate alarm",
        environment="dev",
        occurrences=1,
        affected_users=0,
        release="sha-abc123",
        trace_id="trace-1",
        first_seen=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    bundle = build_evidence_bundle(
        signal,
        logs_url="https://console.aws.amazon.com/cloudwatch/logs?q=trace-1",
        cloudwatch_alarm_url="https://console.aws.amazon.com/cloudwatch/alarm/dev-test",
        reproduction="Use the synthetic dev-only canary that emits controlled-test:chat-error-rate.",
        customer_impact="No customer impact; controlled dev signal.",
        run_id="run-123",
    )

    assert bundle["schema"] == "isol8.reliability.evidence.v1"
    assert bundle["severity"] == Severity.SEV4.value
    assert bundle["dedupe_key"] == "reliability:cloudwatch:dev:controlled-test:chat-error-rate"
    assert bundle["required_human_gates"] == [
        "merge",
        "deploy",
        "destructive_remediation",
        "sev1_or_secret_handling",
    ]
    assert is_controlled_test_bundle(bundle) is True


def test_prod_repeated_signal_escalates_and_uses_stable_dedupe_key():
    signal = ReliabilitySignal(
        source=SignalSource.SENTRY,
        fingerprint="issue-42",
        title="Checkout failure",
        environment="prod",
        occurrences=12,
        affected_users=2,
    )

    assert classify_severity(signal) == Severity.SEV2
    assert dedupe_key(signal) == "reliability:sentry:prod:issue-42"


def test_secret_signal_is_sev1_and_not_a_controlled_test():
    signal = ReliabilitySignal(
        source=SignalSource.CLOUDWATCH,
        fingerprint="controlled-test:leaky-log",
        title="Synthetic secret leak",
        environment="dev",
        contains_secret=True,
    )

    bundle = build_evidence_bundle(signal)

    assert bundle["severity"] == Severity.SEV1.value
    assert bundle["redactions"] == ["source reported possible secret; raw payload withheld"]
    assert is_controlled_test_bundle(bundle) is False
