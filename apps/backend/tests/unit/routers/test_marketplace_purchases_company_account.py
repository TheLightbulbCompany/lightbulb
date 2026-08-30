import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import marketplace_purchases as mod  # noqa: E402


@pytest.mark.asyncio
async def test_account_updated_for_company_account_writes_status_not_seller_flush():
    account = {
        "id": "acct_co",
        "details_submitted": True,
        "charges_enabled": True,
        "payouts_enabled": False,
        "metadata": {"owner_id": "org_a", "kind": "company"},
    }
    with (
        patch.object(
            mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value={"owner_id": "org_a"})
        ),
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value={})) as set_status,
        patch.object(mod, "_flush_seller_held_balance", AsyncMock()) as flush,
        patch.object(mod, "write_owner_facts", AsyncMock()) as facts,
    ):
        await mod._handle_account_updated(account)
    set_status.assert_awaited_once_with(
        "org_a", account_id="acct_co", details_submitted=True, charges_enabled=True, payouts_enabled=False
    )
    flush.assert_not_awaited()
    # The container decides whether to ask for Stripe off the facts file.
    facts.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_account_updated_for_seller_account_keeps_seller_path():
    account = {"id": "acct_seller", "payouts_enabled": True, "metadata": {"seller_id": "user_s"}}
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(
            mod.marketplace_repo,
            "get_payout_account",
            AsyncMock(return_value={"stripe_connect_account_id": "acct_seller"}),
        ),
        patch.object(mod, "_flush_seller_held_balance", AsyncMock()) as flush,
    ):
        await mod._handle_account_updated(account)
    flush.assert_awaited_once_with(seller_id="user_s", connect_account_id="acct_seller")


@pytest.mark.asyncio
async def test_account_deauthorized_clears_binding():
    with (
        patch.object(
            mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value={"owner_id": "org_a"})
        ),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
        patch.object(mod, "write_owner_facts", AsyncMock()) as facts,
    ):
        await mod._handle_account_deauthorized("acct_co")
    clear.assert_awaited_once_with("org_a", account_id="acct_co")
    facts.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_account_deauthorized_unknown_account_is_noop():
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
        patch.object(mod, "write_owner_facts", AsyncMock()),
    ):
        await mod._handle_account_deauthorized("acct_nobody")
        await mod._handle_account_deauthorized(None)
    clear.assert_not_awaited()
