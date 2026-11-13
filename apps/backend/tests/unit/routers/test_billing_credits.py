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
async def test_legacy_credit_checkout_and_auto_charge_routes_are_gone(async_client):
    for path in (
        "/api/v1/billing/credits/top_up",
        "/api/v1/billing/credits/top_up_checkout",
        "/api/v1/billing/credits/auto_charge/disable",
    ):
        response = await async_client.post(path, json={"amount_cents": 2000})
        assert response.status_code == 404
