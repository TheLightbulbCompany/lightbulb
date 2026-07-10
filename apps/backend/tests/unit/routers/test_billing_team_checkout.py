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


@pytest.mark.asyncio
async def test_team_checkout_happy_path(monkeypatch):
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=4))
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        result = await billing.create_team_checkout(auth=_admin_ctx())

    assert result == {"checkout_url": "https://checkout/team"}
    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)


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
