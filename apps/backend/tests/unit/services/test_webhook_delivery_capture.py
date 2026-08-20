import base64
import json
import logging

from starlette.datastructures import Headers

from core.config import settings
from core import webhook_delivery_capture as capture


def test_builds_stripe_capture_record_with_raw_body_and_signature_headers():
    body = b'{"id":"evt_123","type":"invoice.payment_failed","created":1787211000}'
    record = capture.build_capture_record(
        body=body,
        headers=Headers({"Stripe-Signature": "t=1,v1=abc", "Content-Type": "application/json", "Authorization": "secret"}),
        source="paperclip_trigger_proxy",
        owner_id="owner_1",
        trigger_public_id="pub_1",
        route="/hooks/t/owner_1/pub_1",
    )

    assert record["capture_schema"] == "isol8.webhook_delivery.v1"
    assert record["provider"] == "stripe"
    assert record["provider_event_type"] == "invoice.payment_failed"
    assert record["provider_delivery_id"] == "evt_123"
    assert record["provider_delivery_timestamp"] == "1787211000"
    assert record["signature_headers"] == {"stripe-signature": "t=1,v1=abc"}
    assert base64.b64decode(record["raw_payload_base64"]) == body
    assert "authorization" not in record["signature_headers"]


def test_builds_meta_capture_record_with_fields_and_stable_fallback_delivery_id():
    body = json.dumps({"object": "page", "entry": [{"id": "p1", "time": 1787211001, "changes": [{"field": "feed"}]}]}).encode()
    record = capture.build_capture_record(
        body=body,
        headers=Headers({"X-Hub-Signature-256": "sha256=abc"}),
        source="meta_webhook",
        route="/api/v1/meta/webhook",
    )

    assert record["provider"] == "meta"
    assert record["provider_event_types"] == ["page.feed"]
    assert record["provider_delivery_id"].startswith("sha256:")
    assert record["provider_delivery_timestamp"] == "1787211001"
    assert record["signature_headers"] == {"x-hub-signature-256": "sha256=abc"}


def test_log_capture_record_is_gated(monkeypatch, caplog):
    record = {"capture_schema": "isol8.webhook_delivery.v1", "raw_payload_base64": "e30="}
    caplog.set_level(logging.INFO, logger=capture.logger.name)

    monkeypatch.setattr(settings, "WEBHOOK_DELIVERY_CAPTURE_LOG_ENABLED", False)
    capture.log_capture_record(record)
    assert capture.CAPTURE_LOG_PREFIX not in caplog.text

    monkeypatch.setattr(settings, "WEBHOOK_DELIVERY_CAPTURE_LOG_ENABLED", True)
    capture.log_capture_record(record)
    assert capture.CAPTURE_LOG_PREFIX in caplog.text
    assert "raw_payload_base64" in caplog.text
