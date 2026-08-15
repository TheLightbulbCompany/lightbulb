import json
from datetime import UTC, datetime

import httpx
import pytest

from core.services.reliability_pipeline import (
    ReliabilityPaperclipClient,
    ReliabilityPipelineConfig,
    issue_payload,
    normalize_cloudwatch_event,
    normalize_sentry_event,
    redact_secrets,
    verify_sentry_signature,
)


def test_redacts_secret_keys_and_high_entropy_values():
    bundle = redact_secrets({"Authorization": "Bearer verysecret", "ok": "x" * 40, "nested": {"token": "abc"}})
    assert bundle["Authorization"] == "[REDACTED]"
    assert bundle["ok"] == "[REDACTED]"
    assert bundle["nested"]["token"] == "[REDACTED]"


def test_sentry_signal_normalizes_severity_and_payload():
    signal = normalize_sentry_event(
        {
            "action": "created",
            "data": {
                "project": {"slug": "backend"},
                "issue": {"id": "ISSUE-1", "title": "Boom", "level": "fatal"},
                "event": {"environment": "staging", "culprit": "jobs.worker", "headers": {"Authorization": "secret"}},
            },
        },
        received_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert signal.source == "sentry"
    assert signal.severity == "critical"
    assert signal.service == "backend"
    assert signal.environment == "staging"
    assert signal.fingerprint


def test_cloudwatch_signal_normalizes_alarm():
    signal = normalize_cloudwatch_event(
        {
            "id": "evt-1",
            "account": "123456789012",
            "region": "us-east-1",
            "detail": {"alarmName": "prod-api-5xx", "state": {"value": "ALARM", "reason": "threshold"}},
        },
        received_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert signal.source == "cloudwatch"
    assert signal.severity == "critical"
    assert signal.fingerprint
    assert signal.provenance["eventId"] == "evt-1"


def test_issue_payload_carries_policy_and_provenance():
    signal = normalize_cloudwatch_event({"detail": {"alarmName": "dev-worker-latency", "state": {"value": "ALARM"}}})
    payload = issue_payload(signal, ReliabilityPipelineConfig("http://paperclip", "key", "company", "jarvis"))
    assert payload["assigneeAgentId"] == "jarvis"
    assert payload["status"] == "todo"
    assert signal.fingerprint in payload["description"]
    assert "human review required" in payload["description"]
    assert "adapterRetries" in payload["description"]


@pytest.mark.asyncio
async def test_client_dedups_before_create():
    signal = normalize_cloudwatch_event({"detail": {"alarmName": "prod-api-5xx", "state": {"value": "ALARM"}}})
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"issues": [{"id": "issue-1", "description": signal.fingerprint}]})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ReliabilityPaperclipClient(
            ReliabilityPipelineConfig("http://paperclip", "key", "company", "jarvis"), http_client=client
        ).route(signal)

    assert result == {"status": "deduped", "issueId": "issue-1", "dedupKey": signal.fingerprint}
    assert [r.method for r in requests] == ["GET"]


@pytest.mark.asyncio
async def test_client_creates_issue_with_idempotency_key():
    signal = normalize_sentry_event({"data": {"project": {"slug": "api"}, "issue": {"title": "Oops"}}})
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"issues": []})
        seen["body"] = json.loads(request.content)
        seen["idempotency"] = request.headers.get("Idempotency-Key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(201, json={"id": "new-1", "identifier": "ISOA-999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ReliabilityPaperclipClient(
            ReliabilityPipelineConfig("http://paperclip", "key", "company", "jarvis"), http_client=client
        ).route(signal)

    assert result["status"] == "created"
    assert result["identifier"] == "ISOA-999"
    assert seen["body"]["assigneeAgentId"] == "jarvis"
    assert seen["idempotency"] == f"reliability:{signal.fingerprint}"
    assert seen["auth"] == "Bearer key"


def test_sentry_signature_verification():
    import hashlib
    import hmac

    raw = b'{"ok":true}'
    sig = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    assert verify_sentry_signature(raw, sig, "secret")
    assert not verify_sentry_signature(raw, "bad", "secret")
