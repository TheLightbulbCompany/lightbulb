"""POST /billing/front-desk: add the add-on to a live subscription, or send to Checkout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers import billing

LIVE = {"owner_id": "org_1", "stripe_customer_id": "cus_1", "stripe_subscription_status": "active"}


@pytest.fixture(autouse=True)
def has_front_desk_agent(monkeypatch):
    """The precheck reads the Paperclip row; default to a workspace that has the agent."""
    row = MagicMock(starter_agent_ids={"customer_operations": "agent_co"})
    resolve = AsyncMock(return_value=row)
    monkeypatch.setattr("core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row", resolve)
    return resolve


def _admin():
    return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin", email="admin@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["added", "already"])
async def test_post_on_live_subscription_adds(outcome, monkeypatch):
    monkeypatch.setattr(billing, "_get_billing_account", AsyncMock(return_value=LIVE))
    add = AsyncMock(return_value=outcome)
    checkout = AsyncMock()
    with (
        patch("core.services.billing_service.add_front_desk_item", new=add),
        patch("core.services.billing_service.create_workspace_checkout", new=checkout),
        patch("core.services.text_lines.ensure_line", new=AsyncMock()),
    ):
        result = await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin())

    assert result == {"status": outcome}
    add.assert_awaited_once_with("org_1")
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_on_trial_returns_checkout_url(monkeypatch):
    monkeypatch.setattr(
        billing, "_get_billing_account", AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    )
    add = AsyncMock()
    checkout = AsyncMock(return_value={"checkout_url": "https://checkout.test/cs_1"})
    with (
        patch("core.services.billing_service.add_front_desk_item", new=add),
        patch("core.services.billing_service.create_workspace_checkout", new=checkout),
    ):
        result = await billing.create_front_desk(billing.FrontDeskRequest(return_to="app"), auth=_admin())

    assert result == {"status": "checkout", "url": "https://checkout.test/cs_1"}
    checkout.assert_awaited_once_with(
        owner_id="org_1", clerk_user_id="user_admin", plan="workspace", return_to="app", front_desk=True
    )
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_apple_is_409(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={**LIVE, "apple_subscription_status": "active"}),
    )
    add = AsyncMock()
    checkout = AsyncMock()
    with (
        patch("core.services.billing_service.add_front_desk_item", new=add),
        patch("core.services.billing_service.create_workspace_checkout", new=checkout),
    ):
        with pytest.raises(HTTPException) as exc:
            await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin())

    assert exc.value.status_code == 409
    assert exc.value.detail == "apple_managed"
    add.assert_not_awaited()
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_member_is_403():
    member = AuthContext(user_id="user_member", org_id="org_1", org_role="org:member")
    with pytest.raises(HTTPException) as exc:
        await billing.create_front_desk(billing.FrontDeskRequest(), auth=member)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_post_without_customer_operations_is_409(monkeypatch, has_front_desk_agent):
    has_front_desk_agent.return_value = MagicMock(starter_agent_ids={"ceo": "agent_ceo"})
    monkeypatch.setattr(billing, "_get_billing_account", AsyncMock(return_value=LIVE))
    add = AsyncMock()
    checkout = AsyncMock()
    with (
        patch("core.services.billing_service.add_front_desk_item", new=add),
        patch("core.services.billing_service.create_workspace_checkout", new=checkout),
    ):
        with pytest.raises(HTTPException) as exc:
            await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin())

    assert exc.value.status_code == 409
    assert exc.value.detail == "no_front_desk_agent"
    add.assert_not_awaited()
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_on_trial_passes_the_real_plan_check(monkeypatch):
    """The door's plan name must be one create_workspace_checkout accepts (not plan_retired)."""
    from core.services import billing_service

    row = {"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "trial_local"}
    monkeypatch.setattr(billing, "_get_billing_account", AsyncMock(return_value=row))
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="claim"))
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())
    monkeypatch.setattr(billing_service, "seats_for_workspace", AsyncMock(return_value=0))
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([]))),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session,
        "create",
        MagicMock(return_value={"id": "cs_1", "url": "https://checkout.test/cs_1"}),
    )

    result = await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin())

    assert result == {"status": "checkout", "url": "https://checkout.test/cs_1"}


@pytest.mark.asyncio
async def test_already_triggers_ensure_line(monkeypatch):
    from core.services import billing_service, photon

    monkeypatch.setattr(billing, "_get_billing_account", AsyncMock(return_value=LIVE))
    ensure = AsyncMock(return_value="provisioned")
    with (
        patch("core.services.billing_service.add_front_desk_item", new=AsyncMock(return_value="already")),
        patch("core.services.text_lines.ensure_line", new=ensure),
    ):
        assert await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin()) == {"status": "already"}
        ensure.assert_awaited_once_with("org_1")
        for err in (billing_service.BillingServiceError("line_provision_failed"), photon.PhotonError(500, "x")):
            ensure.side_effect = err
            with pytest.raises(HTTPException) as e:
                await billing.create_front_desk(billing.FrontDeskRequest(), auth=_admin())
            assert e.value.status_code == 502 and e.value.detail == "line_provision_failed"
