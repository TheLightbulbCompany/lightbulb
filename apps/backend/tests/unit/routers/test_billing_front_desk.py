"""POST /billing/front-desk: add the add-on to a live subscription, or send to Checkout."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers import billing

LIVE = {"owner_id": "org_1", "stripe_customer_id": "cus_1", "stripe_subscription_status": "active"}


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
        owner_id="org_1", clerk_user_id="user_admin", plan="standard", return_to="app", front_desk=True
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
