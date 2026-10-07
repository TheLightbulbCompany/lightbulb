"""Integration test: replayed Stripe webhooks are no-ops thanks to dedup."""

import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _stub_reconcile_dispatch(monkeypatch):
    """Live subscription events dispatch a durable reconcile (a Step Functions execution), and a
    dispatch that cannot start raises so Stripe redelivers. Stub it so no test reaches AWS."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    return dispatch


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
        monkeypatch.setattr("core.config.settings.STRIPE_WORKSPACE_PRICE_ID", "price_workspace_test")
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

    # Spy on the underlying repo write that the handler triggers via
    # billing_repo.set_subscription on customer.subscription.updated.
    with (
        patch(
            "core.repositories.billing_repo.set_subscription",
            new=AsyncMock(return_value={}),
        ) as mock_write,
        patch(
            "core.repositories.billing_repo.list_by_stripe_customer_id",
            new=AsyncMock(
                return_value=[
                    {
                        "owner_id": "user_replay_test",
                        "stripe_customer_id": "cus_x",
                    }
                ]
            ),
        ),
    ):
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
    assert mock_write.await_count == 1, f"Expected 1 underlying write, got {mock_write.await_count}"
