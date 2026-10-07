"""A Connect `account.application.deauthorized` event must never clear a
row bound by API key — same account id, different relationship."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import marketplace_purchases as mod  # noqa: E402


@pytest.mark.asyncio
async def test_stale_connect_deauth_leaves_an_api_key_row_alone():
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "api_key"}
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=row)),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
        patch.object(mod, "write_owner_facts", AsyncMock()) as facts,
    ):
        await mod._handle_account_deauthorized("acct_x")
    clear.assert_not_awaited()
    facts.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_deauth_still_clears_a_connected_row():
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "connected"}
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=row)),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
        patch.object(mod, "write_owner_facts", AsyncMock()),
    ):
        await mod._handle_account_deauthorized("acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
