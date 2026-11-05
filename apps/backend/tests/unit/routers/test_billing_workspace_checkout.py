"""POST /billing/checkout workspace-only authorization and response contract."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers import billing


def _admin():
    return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin", email="admin@example.com")


def test_legacy_subscription_checkout_routes_are_not_registered():
    paths = {route.path for route in billing.router.routes}
    assert "/trial-checkout" not in paths
    assert "/team-checkout" not in paths
    assert "/ultra-checkout" not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["workspace"])
async def test_org_admin_can_start_workspace_checkout(plan, monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    create = AsyncMock(return_value={"checkout_url": "https://checkout.test/cs_1"})
    with patch("core.services.billing_service.create_workspace_checkout", new=create):
        result = await billing.create_workspace_checkout(billing.CheckoutRequest(plan=plan), auth=_admin())

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    create.assert_awaited_once_with(
        owner_id="org_1", clerk_user_id="user_admin", plan=plan, return_to="web", confirm=False, then=None
    )


@pytest.mark.asyncio
async def test_workspace_checkout_passes_return_to_app_through(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    create = AsyncMock(return_value={"checkout_url": "https://checkout.test/cs_1"})
    with patch("core.services.billing_service.create_workspace_checkout", new=create):
        result = await billing.create_workspace_checkout(
            billing.CheckoutRequest(plan="workspace", return_to="app", confirm=False), auth=_admin()
        )

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    create.assert_awaited_once_with(
        owner_id="org_1", clerk_user_id="user_admin", plan="workspace", return_to="app", confirm=False, then=None
    )


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_an_unknown_return_to(async_client):
    resp = await async_client.post("/api/v1/billing/checkout", json={"plan": "workspace", "return_to": "desktop"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_a_member():
    member = AuthContext(user_id="user_member", org_id="org_1", org_role="org:member")
    with pytest.raises(HTTPException) as exc:
        await billing.create_workspace_checkout(billing.CheckoutRequest(plan="workspace"), auth=member)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_workspace_checkout_requires_an_active_workspace():
    with pytest.raises(HTTPException) as exc:
        await billing.create_workspace_checkout(
            billing.CheckoutRequest(plan="workspace"), auth=AuthContext(user_id="user_1")
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "workspace_required"


@pytest.mark.asyncio
async def test_workspace_checkout_returns_same_plan_noop(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    create = AsyncMock(return_value={"updated": False})
    with patch("core.services.billing_service.create_workspace_checkout", new=create):
        result = await billing.create_workspace_checkout(billing.CheckoutRequest(plan="workspace"), auth=_admin())

    assert result == {"updated": False}


@pytest.mark.asyncio
async def test_workspace_checkout_refuses_an_apple_billed_workspace(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(
            return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "apple_subscription_status": "active"}
        ),
    )
    create = AsyncMock(return_value={"checkout_url": "https://checkout.test/cs_1"})
    with patch("core.services.billing_service.create_workspace_checkout", new=create):
        with pytest.raises(HTTPException) as exc:
            await billing.create_workspace_checkout(billing.CheckoutRequest(plan="workspace"), auth=_admin())

    assert exc.value.status_code == 409
    assert exc.value.detail == "apple_managed"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_checkout_passes_the_plan_change_confirmation_through(monkeypatch):
    create = AsyncMock(return_value={"updated": True})
    monkeypatch.setattr(billing, "_get_billing_account", AsyncMock(return_value={"stripe_customer_id": "cus_1"}))
    with patch("core.services.billing_service.create_workspace_checkout", create):
        result = await billing.create_workspace_checkout(
            billing.CheckoutRequest(plan="workspace", confirm=True), auth=_admin()
        )

    assert result == {"updated": True}
    assert create.await_args.kwargs["confirm"] is True
