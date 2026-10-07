"""GET /billing/spend — always tokens_only; admin-gated like /billing/usage."""

import pytest


@pytest.mark.asyncio
async def test_spend_endpoint_is_tokens_only(async_client):
    resp = await async_client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_spend_endpoint_rejects_non_admin_org_member(async_client_org_member):
    """A non-admin member must not read the org's billing-account spend detail."""
    resp = await async_client_org_member.get("/api/v1/billing/spend")
    assert resp.status_code == 403
