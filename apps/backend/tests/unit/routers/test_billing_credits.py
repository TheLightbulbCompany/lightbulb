"""Credit routes use workspace-owned, Metronome payment-gated funding."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.webhook_dedup import TopUpIntentConflict


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
        patch(
            "core.services.webhook_dedup.get_active_top_up",
            new=AsyncMock(return_value={"request_id": REQUEST_ID, "amount_cents": 2000, "status": "requested"}),
        ),
    ):
        response = await async_client.get("/api/v1/billing/credits/balance")

    assert response.status_code == 200
    assert response.json() == {
        "balance_microcents": 12_340_000,
        "balance_dollars": "12.34",
        "is_exhausted": False,
        "active_top_up": {"request_id": REQUEST_ID, "amount_cents": 2000, "status": "requested"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_state", "pointer_matches", "pointer_status", "expected_status"),
    [
        ("paid", False, "pending", "paid"),
        ("completed", False, "pending", "requested"),
        ("completed", True, "action_required", "action_required"),
    ],
)
async def test_get_balance_reconciles_the_callers_exact_top_up(
    async_client, intent_state, pointer_matches, pointer_status, expected_status
):
    exact_request = "8e22fb41-07e3-4f9d-920c-68ce9a7710b7"
    exact_event = f"metronome-top-up:org_test_456:{exact_request}"
    with (
        patch("core.services.metronome_gate.read_net_balance_microcents", new=AsyncMock(return_value=0)),
        patch("core.services.metronome_gate.apply_fresh_balance", new=AsyncMock()),
        patch(
            "core.services.webhook_dedup.get_active_top_up",
            new=AsyncMock(
                return_value={
                    "intent_event_id": exact_event
                    if pointer_matches
                    else "metronome-top-up:org_test_456:another-request",
                    "request_id": exact_request if pointer_matches else "another-request",
                    "amount_cents": 5000,
                    "status": pointer_status,
                }
            ),
        ),
        patch(
            "core.services.webhook_dedup._read_event",
            new=AsyncMock(
                return_value={
                    "event_id": exact_event,
                    "kind": "metronome-top-up",
                    "owner_id": "org_test_456",
                    "request_id": exact_request,
                    "amount_cents": 2000,
                    "state": intent_state,
                }
            ),
        ),
    ):
        response = await async_client.get(
            "/api/v1/billing/credits/balance",
            params={"request_id": exact_request},
        )

    assert response.status_code == 200
    assert response.json()["active_top_up"] == {
        "request_id": exact_request,
        "amount_cents": 2000,
        "status": expected_status,
    }


@pytest.mark.asyncio
async def test_get_balance_rejects_an_invalid_reconciliation_request_id(async_client):
    response = await async_client.get(
        "/api/v1/billing/credits/balance",
        params={"request_id": "not-a-uuid"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_balance_does_not_fall_back_to_another_admins_top_up(async_client):
    with (
        patch("core.services.metronome_gate.read_net_balance_microcents", new=AsyncMock(return_value=0)),
        patch("core.services.metronome_gate.apply_fresh_balance", new=AsyncMock()),
        patch(
            "core.services.webhook_dedup.get_active_top_up",
            new=AsyncMock(
                return_value={
                    "request_id": "another-request",
                    "amount_cents": 5000,
                    "status": "pending",
                }
            ),
        ),
        patch("core.services.webhook_dedup._read_event", new=AsyncMock(return_value={})),
    ):
        response = await async_client.get(
            "/api/v1/billing/credits/balance",
            params={"request_id": "9e22fb41-07e3-4f9d-920c-68ce9a7710b7"},
        )

    assert response.status_code == 200
    assert response.json()["active_top_up"] is None


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
async def test_reused_top_up_uuid_with_different_binding_is_a_controlled_conflict(async_client):
    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(side_effect=TopUpIntentConflict("top-up intent binding changed")),
    ):
        response = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000, "request_id": REQUEST_ID},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "top-up intent binding changed"


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
