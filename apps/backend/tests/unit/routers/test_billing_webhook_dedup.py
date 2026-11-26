"""Integration test: replayed Stripe webhooks are no-ops thanks to dedup."""

import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def dedup_table_and_settings(monkeypatch):
    """Provision a moto-mocked dedup table and point the Settings at it."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-webhook-event-dedup",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")
        yield


@pytest.mark.asyncio
async def test_replayed_stripe_webhook_processed_once(dedup_table_and_settings, async_client, monkeypatch):
    """Same event.id POSTed twice => underlying handler runs once."""

    fake_event = {
        "id": "evt_replay_test_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "customer": "cus_x",
                "id": "sub_x",
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": "si_workspace",
                            "price": {
                                "id": "price_workspace_test",
                                "recurring": {"interval": "month"},
                            },
                        }
                    ]
                },
            }
        },
    }

    # Bypass Stripe signature verification — we're testing dedup, not auth.
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )

    # Spy on the Spark handler.
    handler = AsyncMock(return_value=None)
    with patch("routers.billing.os_billing.stripe_handler", return_value=handler):
        body = json.dumps(fake_event)
        first = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"stripe-signature": "ignored"},
        )
        second = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"stripe-signature": "ignored"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert handler.await_count == 1, f"Expected 1 handler run, got {handler.await_count}"
