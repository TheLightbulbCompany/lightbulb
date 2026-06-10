"""Unit tests for POST /webhooks/metronome (signature + flag set + kill-switch)."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services.webhook_dedup import WebhookDedupResult
from routers.metronome_webhook import router

SECRET = "whsec_metronome_test"
DATE = "Mon, 08 Jun 2026 00:00:00 GMT"

# The webhook payload carries the METRONOME customer UUID, never our owner_id;
# the handler resolves the owner through the billing row.
METRONOME_CUSTOMER_ID = "ac39ecc3-87ee-4d58-8ec0-24041464dddd"
OWNER_ID = "org_abc"


def _sign(body: bytes) -> str:
    msg = DATE.encode("utf-8") + b"\n" + body
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def known_customer():
    with patch(
        "routers.metronome_webhook.billing_repo.get_by_metronome_customer_id",
        new=AsyncMock(
            side_effect=lambda cid: (
                {"owner_id": OWNER_ID, "metronome_customer_id": cid} if cid == METRONOME_CUSTOMER_ID else None
            )
        ),
    ):
        yield


def _killer(*, already_stopped: bool = False) -> MagicMock:
    killer = MagicMock()
    killer.kill_owner_bedrock = AsyncMock()
    killer.is_owner_service_stopped = AsyncMock(return_value=already_stopped)
    return killer


def _low_balance_body(customer_id=METRONOME_CUSTOMER_ID, remaining_balance=123, **extra_props):
    # remaining_balance / threshold are Metronome's USD credit type: CENTS.
    return json.dumps(
        {
            "id": "evt-1",
            "type": "alerts.low_remaining_credit_balance_reached",
            "properties": {
                "customer_id": customer_id,
                "remaining_balance": remaining_balance,
                "threshold": 500,
                **extra_props,
            },
        }
    ).encode("utf-8")


def _post(client, body: bytes):
    return client.post(
        "/webhooks/metronome",
        content=body,
        headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
    )


def test_rejects_bad_signature(client):
    body = _low_balance_body()
    resp = client.post(
        "/webhooks/metronome",
        content=body,
        headers={"Metronome-Webhook-Signature": "deadbeef", "Date": DATE},
    )
    assert resp.status_code == 401


def test_sets_fast_gate_flag_resolving_owner_from_billing_row(client, known_customer):
    body = _low_balance_body(remaining_balance=123)  # 123 cents -> 1_230_000 microcents (a 100x slip would show)
    killer = _killer()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("core.containers.get_ecs_manager", return_value=killer),
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=1_230_000)
    # Positive balance: alert fired early (threshold crossing), no kill.
    killer.kill_owner_bedrock.assert_not_called()


def test_non_positive_balance_fires_kill_when_enforcement_live(client, known_customer, monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    body = _low_balance_body(remaining_balance=0)
    killer = _killer()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("core.containers.get_ecs_manager", return_value=killer),
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=0)
    killer.kill_owner_bedrock.assert_awaited_once_with(OWNER_ID)


def test_non_positive_balance_no_kill_when_enforcement_off(client, known_customer, monkeypatch):
    """Shadow mode (the default): flag still set, kill never fires."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", False)
    body = _low_balance_body(remaining_balance=-100)
    killer = _killer()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("core.containers.get_ecs_manager", return_value=killer),
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=-1_000_000)
    killer.kill_owner_bedrock.assert_not_called()


def test_unknown_customer_acks_200_without_processing(client, known_customer):
    """Webhooks must not 500-loop: an unknown Metronome customer is logged
    and acked — redelivery can't fix a missing billing-row mapping."""
    body = _low_balance_body(customer_id="not-a-known-uuid", remaining_balance=0)
    killer = _killer()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("core.containers.get_ecs_manager", return_value=killer),
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
    killer.kill_owner_bedrock.assert_not_called()


def test_replay_is_acked_without_processing(client, known_customer):
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
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()


def test_ignores_unrelated_event_type(client, known_customer):
    body = json.dumps(
        {
            "id": "evt-2",
            "type": "alerts.spend_threshold_reached",
            "properties": {"customer_id": METRONOME_CUSTOMER_ID},
        }
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
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
