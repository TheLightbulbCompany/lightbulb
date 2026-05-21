"""GET /billing/account exposes provider_choice for the frontend picker check.

provider_choice lives on billing_accounts (per-owner). The frontend
reads it here to skip the ProviderPicker when an org has already
been onboarded, and to render the matching LLM settings panel without a
detour through /users/me.
"""

from unittest.mock import AsyncMock, patch

import pytest


def _summary_zero(period: str = "2026-04") -> dict:
    return {
        "period": period,
        "total_spend": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "request_count": 0,
        "lifetime_spend": 0.0,
    }


@pytest.mark.asyncio
@patch("routers.billing.get_usage_summary")
@patch("routers.billing.billing_repo")
async def test_account_returns_provider_choice_from_billing_row(mock_repo, mock_get_summary, async_client):
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_x",
            "owner_type": "personal",
            "stripe_customer_id": "cus_abc",
            "stripe_subscription_id": "sub_x",
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }
    )
    mock_get_summary.return_value = _summary_zero()

    resp = await async_client.get("/api/v1/billing/account")

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_choice"] == "bedrock_claude"
    assert body["byo_provider"] is None


@pytest.mark.asyncio
@patch("routers.billing.get_usage_summary")
@patch("routers.billing.billing_repo")
async def test_account_returns_null_provider_choice_when_unset(mock_repo, mock_get_summary, async_client):
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_y",
            "owner_type": "personal",
            "stripe_customer_id": "cus_def",
            "stripe_subscription_id": "sub_y",
            "subscription_status": "active",
        }
    )
    mock_get_summary.return_value = _summary_zero()

    resp = await async_client.get("/api/v1/billing/account")

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_choice"] is None
    assert body["byo_provider"] is None


@pytest.mark.asyncio
@patch("routers.billing.get_usage_summary")
@patch("routers.billing.billing_repo")
async def test_account_returns_byo_provider_when_byo_key(mock_repo, mock_get_summary, async_client):
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_z",
            "owner_type": "personal",
            "stripe_customer_id": "cus_ghi",
            "stripe_subscription_id": "sub_z",
            "subscription_status": "active",
            "provider_choice": "byo_key",
            "byo_provider": "openai",
        }
    )
    mock_get_summary.return_value = _summary_zero()

    resp = await async_client.get("/api/v1/billing/account")

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_choice"] == "byo_key"
    assert body["byo_provider"] == "openai"
