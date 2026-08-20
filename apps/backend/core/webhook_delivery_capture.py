"""Opt-in live webhook delivery capture records for verification/replay.

The capture record intentionally preserves the raw bytes and provider signature
headers needed to verify the public webhook event reference against real
provider deliveries. It is off by default; enable only for controlled capture
windows with CloudWatch log access restricted to the verification run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from starlette.datastructures import Headers

from core.config import settings

CAPTURE_LOG_PREFIX = "WEBHOOK_DELIVERY_CAPTURE "
logger = logging.getLogger(__name__)

_SIGNATURE_HEADERS = {
    "stripe-signature",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-github-delivery",
    "x-github-event",
    "x-gitlab-event",
    "x-gitlab-token",
    "x-vercel-id",
    "x-vercel-signature",
    "x-goog-channel-id",
    "x-goog-channel-token",
    "x-goog-message-number",
    "x-goog-resource-id",
    "x-goog-resource-state",
    "x-goog-resource-uri",
    "x-slack-signature",
    "x-slack-request-timestamp",
    "x-shopify-hmac-sha256",
    "x-shopify-topic",
    "svix-id",
    "svix-signature",
    "svix-timestamp",
    "webhook-id",
    "webhook-signature",
    "webhook-timestamp",
}
_TIMESTAMP_HEADERS = ("date", "x-slack-request-timestamp", "svix-timestamp", "webhook-timestamp")


def _header_map(headers: Headers | Mapping[str, str]) -> dict[str, str]:
    items = headers.items() if hasattr(headers, "items") else []
    return {str(k).lower(): str(v) for k, v in items}


def signature_headers(headers: Headers | Mapping[str, str]) -> dict[str, str]:
    """Provider headers required to verify or replay the captured delivery."""
    lowered = _header_map(headers)
    return {name: value for name, value in sorted(lowered.items()) if name in _SIGNATURE_HEADERS}


def _json_body(body: bytes) -> Any | None:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - capture must never affect delivery path
        return None


def _body_value(payload: Any, *keys: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value: Any = payload
        for part in key.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value not in (None, ""):
            return str(value)
    return None


def _meta_event_types(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    object_type = str(payload.get("object") or "meta")
    fields: set[str] = set()
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if isinstance(change, dict) and change.get("field"):
                fields.add(str(change["field"]))
    return [f"{object_type}.{field}" for field in sorted(fields)] or [object_type]


def infer_delivery_metadata(body: bytes, headers: Headers | Mapping[str, str]) -> dict[str, Any]:
    """Best-effort provider event metadata without trusting it for auth."""
    lowered = _header_map(headers)
    payload = _json_body(body)
    event_type = (
        lowered.get("x-github-event")
        or lowered.get("x-gitlab-event")
        or lowered.get("x-shopify-topic")
        or lowered.get("x-goog-resource-state")
        or _body_value(payload, "type", "event", "event_type", "eventType")
    )
    provider = "unknown"
    if "stripe-signature" in lowered:
        provider = "stripe"
    elif "x-hub-signature-256" in lowered and isinstance(payload, dict) and payload.get("object") in {"page", "instagram"}:
        provider = "meta"
    elif "x-github-event" in lowered:
        provider = "github"
    elif any(k.startswith("x-goog-") for k in lowered):
        provider = "google"
    elif "x-vercel-signature" in lowered or "x-vercel-id" in lowered:
        provider = "vercel"

    meta_event_types = _meta_event_types(payload) if provider == "meta" else []
    delivery_id = (
        lowered.get("x-github-delivery")
        or lowered.get("svix-id")
        or lowered.get("webhook-id")
        or lowered.get("x-goog-message-number")
        or _body_value(payload, "id", "event.id", "data.id")
    )
    if not delivery_id:
        digest = hashlib.sha256(body + (lowered.get("stripe-signature") or lowered.get("x-hub-signature-256") or "").encode()).hexdigest()
        delivery_id = f"sha256:{digest}"

    timestamp = _body_value(payload, "created", "created_at", "createdAt", "time")
    if not timestamp and isinstance(payload, dict):
        entries = payload.get("entry") or []
        if entries and isinstance(entries[0], dict) and entries[0].get("time") is not None:
            timestamp = str(entries[0]["time"])
    if not timestamp:
        timestamp = next((lowered[h] for h in _TIMESTAMP_HEADERS if lowered.get(h)), None)

    return {
        "provider": provider,
        "provider_event_type": event_type,
        "provider_event_types": meta_event_types or ([event_type] if event_type else []),
        "provider_delivery_id": delivery_id,
        "provider_delivery_timestamp": timestamp,
    }


def build_capture_record(
    *,
    body: bytes,
    headers: Headers | Mapping[str, str],
    source: str,
    owner_id: str | None = None,
    trigger_public_id: str | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    metadata = infer_delivery_metadata(body, headers)
    return {
        "capture_schema": "isol8.webhook_delivery.v1",
        "source": source,
        "owner_id": owner_id,
        "trigger_public_id": trigger_public_id,
        "route": route,
        **metadata,
        "signature_headers": signature_headers(headers),
        "content_type": _header_map(headers).get("content-type"),
        "raw_payload_base64": base64.b64encode(body).decode("ascii"),
    }


def log_capture_record(record: dict[str, Any]) -> None:
    """Emit one machine-readable CloudWatch line when capture is enabled."""
    if not settings.WEBHOOK_DELIVERY_CAPTURE_LOG_ENABLED:
        return
    logger.info("%s%s", CAPTURE_LOG_PREFIX, json.dumps(record, sort_keys=True, separators=(",", ":")))
