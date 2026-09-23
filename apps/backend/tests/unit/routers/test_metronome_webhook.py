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


# Every event-type spelling the handler must accept: the alerts we create
# are commit-balance alerts, but the webhook event-type naming is not
# documented as 1:1 with the alert-create enum, so the handler set-matches.
ACCEPTED_EVENT_TYPES = (
    "alerts.low_remaining_commit_balance_reached",
    "alerts.low_remaining_credit_balance_reached",
    "alerts.low_remaining_contract_credit_and_commit_balance_reached",
)


def _low_balance_body(
    customer_id=METRONOME_CUSTOMER_ID,
    remaining_balance=123,
    event_type="alerts.low_remaining_commit_balance_reached",
    **extra_props,
):
    # remaining_balance / threshold are Metronome's USD credit type: CENTS.
    return json.dumps(
        {
            "id": "evt-1",
            "type": event_type,
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


@pytest.mark.parametrize("event_type", ACCEPTED_EVENT_TYPES)
def test_sets_fast_gate_flag_resolving_owner_from_billing_row(client, known_customer, event_type):
    # 123 cents -> 1_230_000 microcents (a 100x slip would show)
    body = _low_balance_body(remaining_balance=123, event_type=event_type)
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
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=1_230_000)


def test_unknown_customer_acks_200_without_processing(client, known_customer):
    """Webhooks must not 500-loop: an unknown Metronome customer is logged
    and acked — redelivery can't fix a missing billing-row mapping."""
    body = _low_balance_body(customer_id="not-a-known-uuid", remaining_balance=0)
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


def test_processing_failure_rolls_back_dedup_and_reraises(client, known_customer):
    """Alerts fire once per crossing — a transient processing failure must
    NOT leave the dedup row in place, or Metronome's redelivery would be
    silently skipped and the one low-balance signal lost forever."""
    body = _low_balance_body()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch(
            "routers.metronome_webhook.webhook_dedup.delete_event",
            new=AsyncMock(),
        ) as delete_mock,
        patch(
            "routers.metronome_webhook.billing_repo.get_by_metronome_customer_id",
            new=AsyncMock(side_effect=RuntimeError("ddb blip")),
        ),
    ):
        with pytest.raises(RuntimeError, match="ddb blip"):
            _post(client, body)
    delete_mock.assert_awaited_once_with("evt-1")


def test_stale_event_skips_set_balance(client, known_customer):
    """A late-delivered low-balance event (older than the fast-gate row's
    updated_at, e.g. a redelivery racing an add_credit re-arm) must not stomp
    a top-up re-arm — a re-revoked paid-up owner emits no usage, so no
    corrective webhook would ever come."""
    body = _low_balance_body(remaining_balance=-100, timestamp="2026-06-08T00:00:00+00:00")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("routers.metronome_webhook.put_metric") as metric_mock,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        # Row re-armed AFTER the event was emitted (top-up landed at 00:05).
        gate.get_row = AsyncMock(return_value={"owner_id": OWNER_ID, "updated_at": "2026-06-08T00:05:00Z"})
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
    assert any(call.args[0] == "metronome.webhook.stale_skipped" for call in metric_mock.call_args_list)


def test_fresh_event_with_older_row_is_processed(client, known_customer):
    """The staleness guard only skips when the row is NEWER than the event —
    the normal case (row from a previous webhook) still applies the update."""
    body = _low_balance_body(remaining_balance=123, timestamp="2026-06-08T00:10:00+00:00")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        gate.get_row = AsyncMock(return_value={"owner_id": OWNER_ID, "updated_at": "2026-06-08T00:00:00Z"})
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=1_230_000)


def test_event_without_timestamp_is_processed_without_row_read(client, known_customer):
    """No event timestamp ⇒ staleness can't be assessed ⇒ process normally
    (dropping the one low-balance signal is the worse failure mode)."""
    body = _low_balance_body(remaining_balance=123)
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        gate.get_row = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.get_row.assert_not_awaited()
    gate.set_balance.assert_awaited_once_with(OWNER_ID, remaining_microcents=1_230_000)


def test_non_numeric_remaining_balance_acks_200_and_keeps_dedup_row(client, known_customer):
    """A permanently-garbled remaining_balance must NOT raise into the
    catch-all (which rolls the dedup row back) — that would put the same
    unfixable event on infinite redelivery. Ack 200, keep the dedup row,
    emit the malformed metric."""
    body = _low_balance_body(remaining_balance="not-a-number")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch(
            "routers.metronome_webhook.webhook_dedup.delete_event",
            new=AsyncMock(),
        ) as delete_mock,
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch("routers.metronome_webhook.put_metric") as metric_mock,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
    delete_mock.assert_not_awaited()
    assert any(call.args[0] == "metronome.webhook.malformed" for call in metric_mock.call_args_list)


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


def _payment_gate_body(event_type="payment_gate.payment_status", **props):
    return json.dumps(
        {
            "id": "evt-pg-1",
            "type": event_type,
            "properties": {
                "customer_id": METRONOME_CUSTOMER_ID,
                "contract_id": "contract_1",
                "invoice_id": "invoice_1",
                **props,
            },
        }
    ).encode("utf-8")


def _post_payment_gate(client, body):
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.put_metric") as put_metric,
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
        patch(
            "routers.metronome_webhook.billing_repo.get_by_metronome_customer_id",
            new=AsyncMock(return_value={"owner_id": OWNER_ID}),
        ),
        patch("routers.metronome_webhook.webhook_dedup.settle_active_top_up", new=AsyncMock(return_value=False)),
        patch("routers.metronome_webhook.webhook_dedup.set_active_top_up_status", new=AsyncMock(return_value=False)),
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = _post(client, body)
    return resp, put_metric, gate


def _payment_gate_metric_dims(put_metric):
    for call in put_metric.call_args_list:
        if call.args and call.args[0] == "metronome.webhook.payment_gate":
            return call.kwargs.get("dimensions")
    return None


def test_payment_gate_failed_is_observable_not_silent(client):
    """A FAILED gated top-up charge must NOT vanish into a silent 200 — it
    emits a payment_gate metric dimensioned by status and never touches the
    fast gate (these carry no balance)."""
    resp, put_metric, gate = _post_payment_gate(client, _payment_gate_body(payment_status="failed"))
    assert resp.status_code == 200
    assert _payment_gate_metric_dims(put_metric) == {"status": "failed"}
    gate.set_balance.assert_not_awaited()


def test_payment_gate_paid_does_not_alert(client):
    """payment_gate.payment_status fires on EVERY attempt, including paid — a
    successful top-up must be counted (status=paid) but must NOT take the
    needs-attention path, or every normal purchase becomes alert noise."""
    resp, put_metric, _ = _post_payment_gate(client, _payment_gate_body(payment_status="paid"))
    assert resp.status_code == 200
    assert _payment_gate_metric_dims(put_metric) == {"status": "paid"}


def test_payment_gate_pending_action_alerts(client):
    """SCA/3DS pending: no payment_status field ⇒ degrade to the attention
    branch (action_required), never a silent drop."""
    resp, put_metric, _ = _post_payment_gate(
        client, _payment_gate_body(event_type="payment_gate.payment_pending_action_required")
    )
    assert resp.status_code == 200
    assert _payment_gate_metric_dims(put_metric) == {"status": "action_required"}


def test_payment_gate_terminal_status_settles_matching_active_top_up(client):
    settle = AsyncMock(return_value=True)
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch(
            "routers.metronome_webhook.billing_repo.get_by_metronome_customer_id",
            new=AsyncMock(return_value={"owner_id": OWNER_ID}),
        ),
        patch("routers.metronome_webhook.webhook_dedup.settle_active_top_up", new=settle),
        patch("routers.metronome_webhook.put_metric"),
    ):
        response = _post(client, _payment_gate_body(payment_status="paid"))

    assert response.status_code == 200
    settle.assert_awaited_once_with(OWNER_ID, METRONOME_CUSTOMER_ID, "contract_1", "paid", invoice_id="invoice_1")


def test_payment_gate_customer_or_contract_mismatch_does_not_settle(client):
    settle = AsyncMock(return_value=False)
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch(
            "routers.metronome_webhook.billing_repo.get_by_metronome_customer_id",
            new=AsyncMock(return_value={"owner_id": OWNER_ID}),
        ),
        patch("routers.metronome_webhook.webhook_dedup.settle_active_top_up", new=settle),
        patch("routers.metronome_webhook.put_metric"),
    ):
        response = _post(client, _payment_gate_body(payment_status="failed", contract_id="contract_other"))

    assert response.status_code == 200
    settle.assert_awaited_once_with(OWNER_ID, METRONOME_CUSTOMER_ID, "contract_other", "failed", invoice_id="invoice_1")


def test_unhandled_event_type_is_counted_not_silent(client):
    """Every other delivery is acked but COUNTED, so an unmodeled type that
    starts arriving (a payment-gate spelling we didn't anticipate, config
    drift, etc.) is visible in metrics instead of silently dropped."""
    body = json.dumps({"id": "evt-inv-1", "type": "invoice.finalized", "properties": {}}).encode("utf-8")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.put_metric") as put_metric,
    ):
        resp = _post(client, body)
    assert resp.status_code == 200
    assert any(call.args and call.args[0] == "metronome.webhook.unhandled" for call in put_metric.call_args_list)
