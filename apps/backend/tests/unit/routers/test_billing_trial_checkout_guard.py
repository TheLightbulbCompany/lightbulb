"""Tests for the checkout 409 guard on ``POST /billing/trial-checkout``.

The guard blocks a second checkout for any subscription state that has
ever billed or might still bill (``TRIAL_BLOCKED_STATUSES``) — Stripe
Checkout in subscription mode creates a NEW subscription on every
successful return, and checkout now bills immediately, so an unguarded
retry double-charges. The expanded blocklist (beyond
``{"active", "trialing", "past_due"}``) dates from audit C3, when
checkout still carried a 14-day Stripe trial and cancel-before-conversion
could mint unlimited free trials (= unlimited always-on ECS Fargate cost).

These tests pin down the expanded blocklist.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.tenancy_codes import PENDING_ORG_INVITATION


_BLOCKED_LOCAL_STATUSES = [
    "active",
    "trialing",
    "past_due",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "unpaid",
    "paused",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status", _BLOCKED_LOCAL_STATUSES)
@patch("routers.billing.billing_repo")
async def test_trial_checkout_409s_on_blocked_local_status(mock_repo, blocked_status, async_client):
    """Local subscription_status in any blocked state -> 409, no
    new Checkout session created."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "stripe_customer_id": "cus_X",
            "stripe_subscription_id": "sub_X",
            "subscription_status": blocked_status,
        }
    )

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409, f"status={blocked_status} should be blocked but got {resp.status_code}"
    assert resp.json()["detail"].startswith("already_subscribed:")


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.retrieve")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_409s_when_live_stripe_status_is_canceled(
    mock_repo, mock_retrieve, mock_create_checkout, async_client
):
    """Local row missing subscription_status but legacy stripe_subscription_id
    is set; live Stripe call returns ``canceled`` -> 409.

    Pre-fix this fell through and minted a NEW subscription, which is
    the trial-gaming exploit.
    """
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "stripe_customer_id": "cus_X",
            "stripe_subscription_id": "sub_X",
            "subscription_status": None,  # local row not backfilled
        }
    )
    mock_retrieve.return_value = {"status": "canceled"}

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_pending_org_invitation_returns_409(mock_repo, mock_clerk, async_client):
    """Caller in personal context with a pending invite must be redirected
    to /onboarding/invitations, not allowed to subscribe personally."""
    # No prior billing row — gate B fires before billing-row checks.
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(
        return_value=[
            {
                "id": "orginv_pending",
                "public_organization_data": {"name": "Acme Org"},
            }
        ]
    )

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == PENDING_ORG_INVITATION
    assert "Acme Org" in body["detail"]["message"]
    assert body["detail"]["redirect_to"] == "/onboarding/invitations"


@pytest.mark.asyncio
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_pending_invite_no_org_name_falls_back(mock_repo, mock_clerk, async_client):
    """When Clerk's pending invite is missing public_organization_data,
    the message uses the generic 'an organization' fallback."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[{"id": "orginv_no_data"}])

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == PENDING_ORG_INVITATION
    assert "an organization" in body["detail"]["message"]


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("core.services.billing_service.BillingService.create_customer_for_owner")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_no_pending_invitations_passes_gate_b(
    mock_repo, mock_clerk, mock_create_customer, mock_create_checkout, async_client
):
    """Empty pending-invitations list lets the request through Gate B. The
    downstream BillingService is mocked so the test verifies ONLY that Gate
    B invoked the Clerk lookup and did not raise its own 409."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    # Mock the downstream Stripe customer + checkout creation so the test
    # doesn't reach real DDB / Stripe.
    mock_create_customer.return_value = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_customer_id": "cus_test",
    }
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/test"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    # Gate B was invoked (proves it WAS evaluated, not skipped).
    mock_clerk.list_pending_invitations_for_user.assert_awaited_once()
    # Gate B did NOT block — if a 409 fires, it must be from a different guard.
    if resp.status_code == 409:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("code") != PENDING_ORG_INVITATION
    mock_clerk.list_pending_invitations_for_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_trial_checkout_in_org_context_skips_pending_invite_check(app, mock_org_admin_user):
    """Org-context callers (org admins running org trial-checkout) must
    skip the pending-invite check — they're creating an org subscription,
    not a personal one."""
    from core.auth import get_current_user
    from httpx import AsyncClient, ASGITransport

    app.dependency_overrides[get_current_user] = mock_org_admin_user
    try:
        with patch("routers.billing.clerk_admin") as mock_clerk, patch("routers.billing.billing_repo") as mock_repo:
            mock_clerk.list_pending_invitations_for_user = AsyncMock()
            mock_repo.get_by_owner_id = AsyncMock(
                return_value={
                    "owner_id": "org_test_456",
                    "stripe_subscription_id": "sub_existing",
                    "subscription_status": "active",  # forces a 409, but NOT from gate B
                }
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post(
                    "/api/v1/billing/trial-checkout",
                    json={"provider_choice": "bedrock_claude"},
                )
            # Critical assertion: gate B was NOT invoked for org context.
            mock_clerk.list_pending_invitations_for_user.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("core.services.billing_service.BillingService.create_customer_for_owner")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_local_owner_gets_a_stripe_customer_backfilled(
    mock_repo, mock_clerk, mock_create_customer, mock_create_checkout, async_client
):
    """A trial_local row EXISTS but carries no stripe_customer_id, so guarding
    customer creation on ``if not account`` skipped it for exactly the owners
    who needed it — and create_flat_fee_checkout then raised "No Stripe
    customer", 400ing every free user who clicked Upgrade. The guard must key on
    the MISSING FIELD, not the missing row."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "owner_type": "personal",
            "subscription_status": "trial_local",
            # no stripe_customer_id — the trial row writes no stripe_* fields
        }
    )
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_create_customer.return_value = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "subscription_status": "trial_local",
        "stripe_customer_id": "cus_backfilled",
    }
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/x"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200, resp.text
    # The backfill ran for the trial row (this is the fix)...
    mock_create_customer.assert_awaited_once()
    # ...and checkout reached Stripe instead of raising "No Stripe customer".
    mock_create_checkout.assert_called_once()
