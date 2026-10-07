"""GET /billing/account exposes the seat-free workspace billing contract."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import billing  # noqa: E402


@pytest.mark.asyncio
async def test_account_for_workspace_owner_has_workspace_prices_without_seats(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "active"}),
    )
    monkeypatch.setattr(
        billing, "get_usage_summary", AsyncMock(return_value={"total_spend": 0.0, "lifetime_spend": 0.0})
    )
    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    payload = resp.model_dump()
    assert payload["workspace_price_usd"] == 50
    assert payload["ultra_price_usd"] == 150
    assert "seat_count" not in payload
    assert "team_seat_price_usd" not in payload
    assert "team_base_price_usd" not in payload
    assert "is_seat_subscription" not in payload


@pytest.mark.asyncio
async def test_account_exposes_server_managed_inference_capability(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "provider_choice": "bedrock_claude",
            }
        ),
    )
    monkeypatch.setattr(
        billing,
        "get_usage_summary",
        AsyncMock(
            return_value={
                "total_spend": 0.0,
                "lifetime_spend": 0.0,
            }
        ),
    )
    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    assert resp.managed_inference_available is True
