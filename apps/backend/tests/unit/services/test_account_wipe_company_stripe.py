import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import account_wipe as mod  # noqa: E402


@pytest.mark.asyncio
async def test_wipe_deauthorizes_the_freshly_read_account_and_never_deletes_it():
    """The account id comes from a strongly-consistent read AT STEP TIME, not
    from the pre-wipe snapshot: a bind landing during the slow steps above
    would otherwise be missed, leaving the platform attached to the owner's
    Stripe account after the account is gone."""
    steps = []
    with (
        patch.object(
            mod.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "company_stripe_account_id": "acct_fresh"}),
        ) as get_row,
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        await mod._wipe_company_stripe(steps, owner_id="org_a")
    get_row.assert_awaited_once_with("org_a", consistent_read=True)
    deauth.assert_awaited_once_with(account_id="acct_fresh")
    clear.assert_awaited_once_with("org_a", account_id="acct_fresh")
    assert steps[-1].system == "stripe"
    assert steps[-1].action == "deauthorize_company_account"
    assert steps[-1].status == "ok"


@pytest.mark.asyncio
async def test_wipe_skips_when_no_company_account():
    steps = []
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        await mod._wipe_company_stripe(steps, owner_id="org_a")
    assert steps[-1].status == "skipped"


@pytest.mark.asyncio
async def test_wipe_deauthorize_failure_leaves_the_binding_in_place():
    steps = []
    with (
        patch.object(
            mod.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "company_stripe_account_id": "acct_fresh"}),
        ),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(side_effect=RuntimeError("stripe down"))),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        await mod._wipe_company_stripe(steps, owner_id="org_a")
    assert steps[-1].status == "failed"
    clear.assert_not_awaited()
