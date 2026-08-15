from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

SignalSource = Literal["sentry", "cloudwatch"]

_SECRET_KEYS = re.compile(r"(authorization|token|secret|password|api[_-]?key|cookie|x-amz-security-token)", re.I)
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b")


@dataclass(frozen=True)
class ReliabilityPipelineConfig:
    paperclip_api_url: str
    paperclip_api_key: str
    company_id: str
    jarvis_agent_id: str
    owner_agent_id: str = ""
    escalation_agent_id: str = ""
    environment: str = "unknown"
    dedup_window_hours: int = 24
    parent_issue_id: str = ""
    run_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.paperclip_api_url and self.paperclip_api_key and self.company_id and self.jarvis_agent_id)


@dataclass(frozen=True)
class NormalizedSignal:
    source: SignalSource
    title: str
    service: str
    environment: str
    severity: str
    fingerprint: str
    evidence: dict[str, Any]
    provenance: dict[str, Any]


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEYS.search(str(k)) else redact_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        return _HIGH_ENTROPY.sub("[REDACTED]", value)
    return value


def _stable_hash(parts: list[str]) -> str:
    return hashlib.sha256("|".join(p.strip().lower() for p in parts if p).encode()).hexdigest()[:16]


def normalize_sentry_event(payload: dict[str, Any], *, received_at: datetime | None = None) -> NormalizedSignal:
    received_at = received_at or datetime.now(UTC)
    data = payload.get("data") or {}
    event = data.get("event") or payload.get("event") or {}
    project = data.get("project") or payload.get("project") or {}
    issue = data.get("issue") or payload.get("issue") or {}
    level = str(event.get("level") or issue.get("level") or payload.get("level") or "error").lower()
    environment = str(event.get("environment") or payload.get("environment") or "unknown")
    service = str(project.get("slug") or project.get("name") or event.get("logger") or "unknown-service")
    title = str(
        issue.get("title") or event.get("title") or event.get("message") or payload.get("action") or "Sentry signal"
    )[:160]
    culprit = str(event.get("culprit") or event.get("transaction") or "")
    severity = "critical" if level in {"fatal", "critical"} else "high" if level == "error" else "medium"
    fingerprint = _stable_hash(
        ["sentry", service, environment, title, culprit, str(issue.get("id") or event.get("event_id") or "")]
    )
    evidence = redact_secrets(
        {
            "action": payload.get("action"),
            "installation": payload.get("installation"),
            "project": project,
            "issue": issue,
            "event": {
                "event_id": event.get("event_id"),
                "level": event.get("level"),
                "logger": event.get("logger"),
                "platform": event.get("platform"),
                "transaction": event.get("transaction"),
                "culprit": event.get("culprit"),
                "environment": event.get("environment"),
                "release": event.get("release"),
                "timestamp": event.get("timestamp"),
                "tags": event.get("tags"),
                "url": event.get("web_url") or event.get("url"),
            },
        }
    )
    provenance = {"receivedAt": received_at.isoformat(), "source": "sentry", "dedupKey": fingerprint}
    return NormalizedSignal("sentry", title, service, environment, severity, fingerprint, evidence, provenance)


def normalize_cloudwatch_event(payload: dict[str, Any], *, received_at: datetime | None = None) -> NormalizedSignal:
    received_at = received_at or datetime.now(UTC)
    detail = payload.get("detail") or {}
    alarm_name = str(
        detail.get("alarmName") or payload.get("alarmName") or payload.get("detail-type") or "CloudWatch alarm"
    )
    state = detail.get("state") or {}
    previous = detail.get("previousState") or {}
    config = detail.get("configuration") or {}
    metrics = config.get("metrics") or []
    environment = str(detail.get("environment") or payload.get("environment") or payload.get("account") or "unknown")
    service = str(detail.get("service") or (payload.get("resources") or [""])[0] or "aws")
    reason = str(state.get("reason") or "")
    value = str(state.get("value") or detail.get("stateValue") or "")
    severity = (
        "critical"
        if value == "ALARM" and any(x in alarm_name.lower() for x in ["prod", "critical", "5xx", "error"])
        else "high"
        if value == "ALARM"
        else "medium"
    )
    fingerprint = _stable_hash(["cloudwatch", alarm_name, environment, service])
    evidence = redact_secrets(
        {
            "account": payload.get("account"),
            "region": payload.get("region"),
            "resources": payload.get("resources"),
            "alarmName": alarm_name,
            "state": state,
            "previousState": previous,
            "configuration": {"metrics": metrics},
            "reason": reason,
        }
    )
    provenance = {
        "receivedAt": received_at.isoformat(),
        "source": "cloudwatch",
        "dedupKey": fingerprint,
        "eventId": payload.get("id"),
    }
    return NormalizedSignal("cloudwatch", alarm_name, service, environment, severity, fingerprint, evidence, provenance)


def issue_payload(signal: NormalizedSignal, config: ReliabilityPipelineConfig) -> dict[str, Any]:
    escalation = "human review required before merge/deploy/high-risk remediation"
    if signal.severity == "critical" and config.escalation_agent_id:
        escalation += f"; escalate to agent {config.escalation_agent_id}"
    body = {
        "source": signal.source,
        "severity": signal.severity,
        "dedupKey": signal.fingerprint,
        "dedupWindowHours": config.dedup_window_hours,
        "environment": signal.environment,
        "service": signal.service,
        "ownership": {"investigatorAgentId": config.jarvis_agent_id, "ownerAgentId": config.owner_agent_id or None},
        "retryPolicy": {
            "adapterRetries": 3,
            "retryBackoff": "exponential",
            "idempotencyKey": f"reliability:{signal.fingerprint}",
        },
        "escalationPolicy": escalation,
        "evidence": signal.evidence,
        "provenance": signal.provenance,
        "nextAction": "Jarvis investigates evidence, prepares an unmerged PR if remediation is code, and waits for human review/merge/deploy approval.",
    }
    description = "## Reliability signal\n\n```json\n" + json.dumps(body, indent=2, sort_keys=True) + "\n```"
    priority = "high" if signal.severity in {"critical", "high", "medium"} else "low"
    payload = {
        "title": f"[{signal.severity.upper()}][{signal.source}] {signal.title}",
        "description": description,
        "status": "todo",
        "priority": priority,
        "assigneeAgentId": config.jarvis_agent_id,
        "workMode": "standard",
    }
    if config.parent_issue_id:
        payload["parentId"] = config.parent_issue_id
    return payload


class ReliabilityPaperclipClient:
    def __init__(self, config: ReliabilityPipelineConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = http_client

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.paperclip_api_key}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if self.config.run_id:
            headers["X-Paperclip-Run-Id"] = self.config.run_id
        return headers

    async def route(self, signal: NormalizedSignal) -> dict[str, Any]:
        if not self.config.enabled:
            raise RuntimeError("reliability pipeline is not configured")
        base = self.config.paperclip_api_url.rstrip("/")
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            params = {"q": signal.fingerprint, "status": "todo,in_progress,in_review,blocked"}
            existing = await client.get(
                f"{base}/api/companies/{self.config.company_id}/issues", params=params, headers=self._headers()
            )
            existing.raise_for_status()
            items = existing.json()
            if isinstance(items, dict):
                items = items.get("issues") or items.get("data") or []
            for item in items if isinstance(items, list) else []:
                blob = json.dumps(item, sort_keys=True)
                if signal.fingerprint in blob:
                    return {"status": "deduped", "issueId": item.get("id"), "dedupKey": signal.fingerprint}
            payload = issue_payload(signal, self.config)
            created = await client.post(
                f"{base}/api/companies/{self.config.company_id}/issues",
                json=payload,
                headers=self._headers(idempotency_key=f"reliability:{signal.fingerprint}"),
            )
            created.raise_for_status()
            data = created.json()
            return {
                "status": "created",
                "issueId": data.get("id"),
                "identifier": data.get("identifier"),
                "dedupKey": signal.fingerprint,
            }
        finally:
            if close_client:
                await client.aclose()


def verify_sentry_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
