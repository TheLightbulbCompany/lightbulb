"""When Clerk fires user.updated with a new email, push it to Stripe Customer."""

from unittest.mock import AsyncMock, patch

import pytest
import stripe


@pytest.mark.asyncio
async def test_user_updated_with_new_email_pushes_to_stripe(async_client, monkeypatch):
    fake_account = {
        "owner_id": "u_1",
        "stripe_customer_id": "cus_existing",
    }

    payload = {
        "type": "user.updated",
        "data": {
            "id": "u_1",
            "email_addresses": [{"id": "ea_1", "email_address": "new@example.com"}],
            "primary_email_address_id": "ea_1",
        },
    }

    # Bypass the handler's svix HMAC signature check — we're testing the
    # downstream sync, not signature verification.
    monkeypatch.setattr(
        "routers.webhooks._verify_svix_signature",
        lambda body, headers: None,
    )

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=fake_account),
        ),
        patch("stripe.Customer.modify") as mock_stripe_modify,
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers={
                "svix-id": "msg_test",
                "svix-timestamp": "1234567890",
                "svix-signature": "ignored",
            },
        )

    assert resp.status_code == 200
    # Idempotency key is keyed on the unique svix-id from the Clerk webhook
    # so that an A→B→A→B email flip doesn't collide with a cached response.
    mock_stripe_modify.assert_called_once_with(
        "cus_existing",
        email="new@example.com",
        idempotency_key="customer_email_sync:msg_test",
    )


@pytest.mark.asyncio
async def test_user_updated_without_stripe_customer_skips_sync(async_client, monkeypatch):
    """If the user has no Stripe customer yet, don't try to push email."""
    payload = {
        "type": "user.updated",
        "data": {
            "id": "u_1",
            "email_addresses": [{"id": "ea_1", "email_address": "x@y.com"}],
            "primary_email_address_id": "ea_1",
        },
    }
    monkeypatch.setattr(
        "routers.webhooks._verify_svix_signature",
        lambda body, headers: None,
    )

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch("stripe.Customer.modify") as mock_stripe_modify,
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers={
                "svix-id": "msg_test",
                "svix-timestamp": "1234567890",
                "svix-signature": "ignored",
            },
        )

    assert resp.status_code == 200
    mock_stripe_modify.assert_not_called()


@pytest.mark.asyncio
async def test_user_updated_stripe_error_is_non_fatal(async_client, monkeypatch):
    """If Stripe.Customer.modify raises, the handler still returns 200 and
    emits the error metric. Stripe sync is best-effort — Clerk has already
    accepted the user.updated event and we don't want it retried purely
    because Stripe was unhappy.
    """
    fake_account = {
        "owner_id": "u_1",
        "stripe_customer_id": "cus_existing",
    }

    payload = {
        "type": "user.updated",
        "data": {
            "id": "u_1",
            "email_addresses": [{"id": "ea_1", "email_address": "new@example.com"}],
            "primary_email_address_id": "ea_1",
        },
    }

    monkeypatch.setattr(
        "routers.webhooks._verify_svix_signature",
        lambda body, headers: None,
    )

    emitted_metrics: list[tuple[str, dict | None]] = []

    def _capture_metric(name, value=1, dimensions=None, unit="Count"):
        emitted_metrics.append((name, dimensions))

    monkeypatch.setattr("routers.webhooks.put_metric", _capture_metric)

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=fake_account),
        ),
        patch(
            "stripe.Customer.modify",
            side_effect=stripe.StripeError("boom"),
        ) as mock_stripe_modify,
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/clerk",
            json=payload,
            headers={
                "svix-id": "msg_test",
                "svix-timestamp": "1234567890",
                "svix-signature": "ignored",
            },
        )

    # Non-fatal: Stripe failure must not turn into a 500 (which would cause
    # Clerk to retry the entire webhook).
    assert resp.status_code == 200
    mock_stripe_modify.assert_called_once()
    assert (
        "stripe.customer.email_sync",
        {"result": "error"},
    ) in emitted_metrics
