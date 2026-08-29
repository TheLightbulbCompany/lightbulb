import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_stripe_approval as mod  # noqa: E402


def _admin(existing: list[dict]):
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value={"approvals": existing})
    admin.create_approval = AsyncMock(return_value={"id": "apr_new"})
    return admin


@pytest.mark.asyncio
async def test_files_connect_stripe_approval():
    admin = _admin(existing=[])
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value="cmp_1")),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
    ):
        approval_id = await mod.request_company_stripe(
            owner_id="org_a", issue_id="iss_1", reason="Need to sell the first product"
        )
    assert approval_id == "apr_new"
    body = admin.create_approval.call_args.kwargs["body"]
    assert body["type"] == "connect_stripe"
    assert body["issueIds"] == ["iss_1"]
    assert body["payload"]["isol8_action"] == "company_stripe"
    assert body["payload"]["title"] == "Set up Northwind Ceramics on Stripe"
    assert body["payload"]["reason"] == "Need to sell the first product"


@pytest.mark.asyncio
async def test_skips_when_already_connected():
    admin = _admin(existing=[])
    with (
        patch.object(
            mod.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "company_stripe_account_id": "acct_x"}),
        ),
        patch.object(mod, "shared_admin_client", return_value=admin),
    ):
        assert await mod.request_company_stripe(owner_id="org_a", issue_id=None, reason="x") is None
    admin.create_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_reuses_pending_request():
    admin = _admin(
        existing=[
            {
                "id": "apr_old",
                "type": "connect_stripe",
                "status": "pending",
                "payload": {"isol8_action": "company_stripe"},
            }
        ]
    )
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value="cmp_1")),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "shared_admin_client", return_value=admin),
    ):
        assert await mod.request_company_stripe(owner_id="org_a", issue_id=None, reason="x") == "apr_old"
    admin.create_approval.assert_not_awaited()
