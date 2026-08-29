import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import account_wipe as mod  # noqa: E402


@pytest.mark.asyncio
async def test_wipe_deauthorizes_company_stripe_account_and_never_deletes_it():
    steps = []
    with (
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        await mod._wipe_company_stripe(steps, owner_id="org_a", billing_row={"company_stripe_account_id": "acct_x"})
    deauth.assert_awaited_once_with(account_id="acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
    assert steps[-1].system == "stripe"
    assert steps[-1].action == "deauthorize_company_account"
    assert steps[-1].status == "ok"


@pytest.mark.asyncio
async def test_wipe_skips_when_no_company_account():
    steps = []
    await mod._wipe_company_stripe(steps, owner_id="org_a", billing_row={})
    assert steps[-1].status == "skipped"
