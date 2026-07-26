"""POST /billing/team-checkout — per-seat Teams subscription."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import billing  # noqa: E402


def _admin_ctx():
    return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin", email="admin@example.com")


def _member_ctx():
    return AuthContext(user_id="user_mem", org_id="org_1", org_role="org:member", email="mem@example.com")


@pytest.fixture(autouse=True)
def _stub_set_provider_choice(monkeypatch):
    """The router now writes provider_choice="bedrock_claude" synchronously
    before Checkout (teams are Bedrock-managed automatically). Stub the DDB
    write for every test in this module; tests that reach it can assert on the
    returned mock."""
    mock = AsyncMock()
    monkeypatch.setattr(billing.billing_repo, "set_provider_choice", mock)
    return mock


@pytest.mark.asyncio
async def test_team_checkout_happy_path(monkeypatch, _stub_set_provider_choice):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_seat_count", AsyncMock(return_value=4))
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        result = await billing.create_team_checkout(auth=_admin_ctx())

    assert result == {"checkout_url": "https://checkout/team"}
    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)
    # Teams are Bedrock-managed automatically — provider_choice persisted
    # synchronously before Checkout so no provider_required flash on return.
    _stub_set_provider_choice.assert_awaited_once_with("org_1", provider_choice="bedrock_claude", byo_provider=None)


@pytest.mark.asyncio
async def test_team_checkout_does_not_set_provider_before_409(monkeypatch, _stub_set_provider_choice):
    """An org with a LIVE sub 409s at the guard, BEFORE the provider write —
    so a re-checkout attempt never rewrites provider_choice on an active org."""
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "active"}),
    )
    with pytest.raises(HTTPException):
        await billing.create_team_checkout(auth=_admin_ctx())
    _stub_set_provider_choice.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_checkout_rejects_non_admin(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=_member_ctx())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_team_checkout_rejects_personal_context():
    personal = AuthContext(user_id="user_p", email="p@example.com")
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=personal)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_team_checkout_409_when_already_subscribed(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "active"}),
    )
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=_admin_ctx())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_team_checkout_503_when_member_count_unknown(monkeypatch):
    # get_organization_seat_count returns 0 on any Clerk error/outage.
    # create_team_seat_checkout floors quantity at max(1, member_count), so
    # without this guard a Clerk outage would subscribe the org at a single
    # seat instead of failing loudly. Must 503, not silently under-bill.
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_seat_count", AsyncMock(return_value=0))
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(),
    ) as mock_create:
        with pytest.raises(HTTPException) as exc:
            await billing.create_team_checkout(auth=_admin_ctx())

    assert exc.value.status_code == 503
    assert exc.value.detail == "team_size_unavailable"
    mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_checkout_allowed_after_cancellation(monkeypatch):
    # A canceled org (sub deleted at Stripe) must be able to self-serve a
    # fresh team-checkout — only a LIVE (non-terminal) subscription should
    # block a new checkout.
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "canceled"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_seat_count", AsyncMock(return_value=4))
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        result = await billing.create_team_checkout(auth=_admin_ctx())

    assert result == {"checkout_url": "https://checkout/team"}
    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)


@pytest.mark.asyncio
async def test_team_checkout_still_blocked_when_active(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "active"}),
    )
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=_admin_ctx())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_team_checkout_quantity_includes_pending_invitations(monkeypatch, _stub_set_provider_choice):
    """No free members: 1 admin + 3 pending invites checks out at 4 seats.

    Stubs the two underlying Clerk reads (member count + pending count) and
    lets the REAL get_organization_seat_count composition run — stubbing
    get_organization_seat_count directly would only prove the router
    forwards whatever it's handed, not that members+pending genuinely sums
    to the checkout quantity."""
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=1))
    monkeypatch.setattr(billing.clerk_admin, "_pending_invitation_count", AsyncMock(return_value=3))
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        await billing.create_team_checkout(auth=_admin_ctx())

    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)
