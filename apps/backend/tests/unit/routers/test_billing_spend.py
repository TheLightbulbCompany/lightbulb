"""GET /billing/spend — provider-aware spend summary endpoint."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_spend_endpoint_bedrock(async_client):
    with (
        patch(
            "routers.billing.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
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
    # the resolved provider_choice is what gets dispatched on
    assert summary.await_args.args[1] == "bedrock_claude"


@pytest.mark.asyncio
async def test_spend_endpoint_owner_with_no_stored_choice_is_tokens_only(async_client):
    """A row with no ``provider_choice`` resolves to the managed gate (never the
    retired bedrock_claude default), which shows no dollars here."""
    billed = AsyncMock(side_effect=AssertionError("no Metronome read for a managed owner"))
    with (
        patch(
            "core.services.container_provision.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "active"}),
        ),
        patch("routers.billing.spend_service.get_billed_month_spend", new=billed),
    ):
        resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "tokens_only"


@pytest.mark.asyncio
async def test_spend_endpoint_trial_local_owner_is_tokens_only(async_client):
    """A no-card trial_local owner resolves to the managed gate,
    which get_spend_summary has no dollar branch for — it must fall through to
    tokens_only. The free tier deliberately shows no dollars."""
    with patch(
        "core.services.container_provision.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"subscription_status": "trial_local"}),
    ):
        resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "tokens_only"


@pytest.mark.asyncio
async def test_spend_endpoint_degrades_to_tokens_only_on_resolver_error(async_client):
    """A DynamoDB throttle/read error surfaced through resolve_provider_choice
    must not 500 a display-only endpoint that promises degraded=tokens_only."""
    with patch(
        "routers.billing.resolve_provider_choice",
        new=AsyncMock(side_effect=RuntimeError("ddb throttled")),
    ):
        resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "tokens_only"


@pytest.mark.asyncio
async def test_spend_endpoint_rejects_non_admin_org_member(async_client_org_member):
    """Same org-member gating as GET /billing/usage — a non-admin member must
    not be able to read the org's billing-account spend detail."""
    with patch(
        "routers.billing.resolve_provider_choice",
        new=AsyncMock(return_value=("bedrock_claude", None)),
    ):
        resp = await async_client_org_member.get("/api/v1/billing/spend")
    assert resp.status_code == 403
