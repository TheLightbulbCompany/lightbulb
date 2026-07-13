"""GET /billing/spend — provider-aware spend summary endpoint."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_spend_endpoint_bedrock(async_client):
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch(
            "routers.billing.spend_service.get_spend_summary",
            new=AsyncMock(
                return_value={
                    "mode": "bedrock",
                    "billed_month_to_date_cents": 1234,
                    "by_model": [{"model": "Claude Opus", "tier": "opus", "cents": 900}],
                    "period_start": "2026-07-01T00:00:00+00:00",
                }
            ),
        ) as summary,
    ):
        resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "bedrock"
    assert body["billed_month_to_date_cents"] == 1234
    assert body["by_model"][0]["tier"] == "opus"
    # provider_choice from the billing row is what gets dispatched on
    assert summary.await_args.args[1] == "bedrock_claude"


@pytest.mark.asyncio
async def test_spend_endpoint_tokens_only_without_billing_row(async_client):
    with patch(
        "routers.billing.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "tokens_only"


@pytest.mark.asyncio
async def test_spend_endpoint_rejects_non_admin_org_member(async_client_org_member):
    """Same org-member gating as GET /billing/usage — a non-admin member must
    not be able to read the org's billing-account spend detail."""
    with patch(
        "routers.billing.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
    ):
        resp = await async_client_org_member.get("/api/v1/billing/spend")
    assert resp.status_code == 403
