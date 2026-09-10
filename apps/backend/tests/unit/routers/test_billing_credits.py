"""Credit routes use workspace-owned, Metronome payment-gated funding."""

from unittest.mock import AsyncMock, patch

import pytest


REQUEST_ID = "7e22fb41-07e3-4f9d-920c-68ce9a7710b7"


@pytest.mark.asyncio
async def test_get_balance_returns_only_metronome_balance(async_client):
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "metro_1"}),
        ),
        patch(
            "core.services.metronome_gate.read_net_balance_microcents",
            new=AsyncMock(return_value=12_340_000),
        ),
        patch("core.services.metronome_gate.apply_fresh_balance", new=AsyncMock()),
    ):
        response = await async_client.get("/api/v1/billing/credits/balance")

    assert response.status_code == 200
    assert response.json() == {
        "balance_microcents": 12_340_000,
        "balance_dollars": "12.34",
        "is_exhausted": False,
    }


@pytest.mark.asyncio
async def test_workspace_admin_requests_payment_gated_top_up(async_client):
    request_top_up = AsyncMock()
    with (
        patch("core.billing.metronome_funding.request_top_up", new=request_top_up),
        patch("core.services.metronome_gate.read_net_balance_microcents", new=AsyncMock(return_value=None)),
    ):
        response = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000, "request_id": REQUEST_ID},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "requested", "amount_cents": 2000}
    request_top_up.assert_awaited_once_with("org_test_456", 2000, request_id=REQUEST_ID)


@pytest.mark.asyncio
async def test_workspace_member_cannot_purchase_credits(async_client_org_member):
    with patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()) as request_top_up:
        response = await async_client_org_member.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000, "request_id": REQUEST_ID},
        )

    assert response.status_code == 403
    request_top_up.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"amount_cents": 499, "request_id": REQUEST_ID},
        {"amount_cents": 100_001, "request_id": REQUEST_ID},
        {"amount_cents": 2000},
        {"amount_cents": 2000, "request_id": "not-a-uuid"},
    ],
)
async def test_top_up_requires_bounded_amount_and_uuid_request_id(async_client, body):
    response = await async_client.post("/api/v1/billing/credits/top_up", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_legacy_credit_checkout_and_auto_charge_routes_are_gone(async_client):
    for path in ("/api/v1/billing/credits/top_up_checkout", "/api/v1/billing/credits/auto_charge/disable"):
        response = await async_client.post(path, json={"amount_cents": 2000})
        assert response.status_code == 404


def test_checkout_events_remain_registered_for_launch_coupon_settlement():
    from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS, handle_checkout_session

    assert STRIPE_WEBHOOK_HANDLERS["checkout.session.completed"] is handle_checkout_session
    assert STRIPE_WEBHOOK_HANDLERS["checkout.session.async_payment_succeeded"] is handle_checkout_session
