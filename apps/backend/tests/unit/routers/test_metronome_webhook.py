"""Unit tests for POST /webhooks/metronome (signature + fast-gate flag set)."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services.webhook_dedup import WebhookDedupResult
from routers.metronome_webhook import router

SECRET = "whsec_metronome_test"
DATE = "Mon, 08 Jun 2026 00:00:00 GMT"


def _sign(body: bytes) -> str:
    msg = DATE.encode("utf-8") + b"\n" + body
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _low_balance_body(owner_id="org_abc", remaining_balance=0.5):
    return json.dumps(
        {
            "id": "evt-1",
            "type": "alerts.low_remaining_credit_balance_reached",
            "properties": {
                "customer_id": owner_id,
                "owner_id": owner_id,
                "remaining_balance": remaining_balance,
                "threshold": 2.0,
            },
        }
    ).encode("utf-8")


def test_rejects_bad_signature(client):
    body = _low_balance_body()
    resp = client.post(
        "/webhooks/metronome",
        content=body,
        headers={"Metronome-Webhook-Signature": "deadbeef", "Date": DATE},
    )
    assert resp.status_code == 401


def test_sets_fast_gate_flag_on_low_balance(client):
    body = _low_balance_body(remaining_balance=0.5)  # $0.50 -> 500_000 microcents
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with("org_abc", remaining_microcents=500_000)


def test_replay_is_acked_without_processing(client):
    body = _low_balance_body()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()


def test_ignores_unrelated_event_type(client):
    body = json.dumps(
        {"id": "evt-2", "type": "alerts.spend_threshold_reached", "properties": {"customer_id": "org_abc"}}
    ).encode("utf-8")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
