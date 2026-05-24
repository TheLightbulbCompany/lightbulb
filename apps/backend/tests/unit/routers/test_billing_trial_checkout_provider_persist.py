"""Race-fix tests for /trial-checkout.

The /trial-checkout endpoint must synchronously persist provider_choice on
billing_accounts BEFORE creating the Stripe Checkout session. Without that
write, there's a race window between Checkout completion and the
customer.subscription.created webhook landing (async, can be
seconds-to-minutes) where the user lands on /chat and triggers
/container/provision, and provision reads billing.provider_choice and finds
nothing.

These tests pin down:
  1. provider_choice (no byo_provider) is persisted before the Stripe call
  2. byo_provider is persisted alongside provider_choice when byo_key
  3. byo_key without byo_provider succeeds (picker submits it alone; the
     BYO wizard fills in byo_provider later)
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_trial_checkout_persists_provider_choice_synchronously(async_client):
    """/trial-checkout must write provider_choice to billing_accounts
    BEFORE creating the Stripe Checkout session, so /container/provision
    can read it without waiting for the async webhook.
    """
    fake_account = {
        "owner_id": "user_x",
        "owner_type": "personal",
        "stripe_customer_id": "cus_abc",
    }

    with (
        patch("routers.billing._get_billing_account", new_callable=AsyncMock, return_value=fake_account),
        patch("routers.billing.billing_repo.set_provider_choice", new_callable=AsyncMock) as mock_set_pc,
        patch(
            "core.services.billing_service.create_flat_fee_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout,
    ):
        mock_checkout.return_value = type("S", (), {"url": "https://checkout.stripe.com/foo"})()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200
    mock_set_pc.assert_awaited_once_with(
        "user_x",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="personal",
    )


@pytest.mark.asyncio
async def test_trial_checkout_persists_byo_provider_when_byo_key(async_client):
    fake_account = {
        "owner_id": "user_y",
        "owner_type": "personal",
        "stripe_customer_id": "cus_def",
    }

    with (
        patch("routers.billing._get_billing_account", new_callable=AsyncMock, return_value=fake_account),
        patch("routers.billing.billing_repo.set_provider_choice", new_callable=AsyncMock) as mock_set_pc,
        patch(
            "core.services.billing_service.create_flat_fee_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout,
    ):
        mock_checkout.return_value = type("S", (), {"url": "https://checkout.stripe.com/foo"})()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

    assert resp.status_code == 200
    mock_set_pc.assert_awaited_once_with(
        "user_y",
        provider_choice="byo_key",
        byo_provider="openai",
        owner_type="personal",
    )


@pytest.mark.asyncio
async def test_trial_checkout_byo_key_without_provider_now_allowed(async_client):
    """The picker submits {provider_choice: 'byo_key'} alone; byo_provider
    gets set later in the BYO wizard step after Stripe checkout completes.
    """
    fake_account = {
        "owner_id": "user_v",
        "owner_type": "personal",
        "stripe_customer_id": "cus_stu",
    }

    with (
        patch("routers.billing._get_billing_account", new_callable=AsyncMock, return_value=fake_account),
        patch("routers.billing.billing_repo.set_provider_choice", new_callable=AsyncMock) as mock_set_pc,
        patch(
            "core.services.billing_service.create_flat_fee_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout,
    ):
        mock_checkout.return_value = type("S", (), {"url": "https://checkout.stripe.com/foo"})()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "byo_key"},  # no byo_provider
        )

    assert resp.status_code == 200
    mock_set_pc.assert_awaited_once_with(
        "user_v",
        provider_choice="byo_key",
        byo_provider=None,
        owner_type="personal",
    )


@pytest.mark.asyncio
async def test_trial_checkout_invalid_provider_choice_rejected(async_client):
    """Unknown provider_choice values are rejected by pydantic Literal
    with 422, before any DDB write or Stripe call."""
    with patch(
        "routers.billing.billing_repo.set_provider_choice",
        new_callable=AsyncMock,
    ) as mock_set_pc:
        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "cohere_claude"},  # not in Literal
        )

    assert resp.status_code == 422
    mock_set_pc.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_checkout_invalid_byo_provider_rejected(async_client):
    """Unknown byo_provider values are rejected by pydantic Literal with
    422 — without this, junk like 'cohere' would be persisted to billing
    then blow up ECS provisioning.
    """
    with patch(
        "routers.billing.billing_repo.set_provider_choice",
        new_callable=AsyncMock,
    ) as mock_set_pc:
        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "byo_key", "byo_provider": "cohere"},
        )

    assert resp.status_code == 422
    mock_set_pc.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_checkout_accepts_valid_byo_provider_values(async_client):
    """Sanity check: openai and anthropic are valid byo_provider values."""
    fake_account = {
        "owner_id": "user_x",
        "owner_type": "personal",
        "stripe_customer_id": "cus_abc",
    }

    for provider in ("openai", "anthropic"):
        with (
            patch("routers.billing._get_billing_account", new_callable=AsyncMock, return_value=fake_account),
            patch("routers.billing.billing_repo.set_provider_choice", new_callable=AsyncMock),
            patch(
                "core.services.billing_service.create_flat_fee_checkout",
                new_callable=AsyncMock,
            ) as mock_checkout,
        ):
            mock_checkout.return_value = type("S", (), {"url": "https://checkout.stripe.com/foo"})()

            resp = await async_client.post(
                "/api/v1/billing/trial-checkout",
                json={"provider_choice": "byo_key", "byo_provider": provider},
            )

        assert resp.status_code == 200, f"byo_provider={provider} should be accepted, got {resp.status_code}"
