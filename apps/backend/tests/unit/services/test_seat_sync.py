"""sync_team_seat_quantity — the single seat-quantity reconciler."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import billing_service  # noqa: E402


@pytest.mark.asyncio
async def test_sets_absolute_member_count_on_mismatch(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
                "subscription_item_id": "si_1",
                "seat_count": 3,
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=5))
    set_team = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", set_team)

    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")

    assert result == 5
    item.modify.assert_called_once()
    _, kwargs = item.modify.call_args
    assert kwargs["quantity"] == 5
    assert item.modify.call_args.args[0] == "si_1"
    set_team.assert_awaited_once()
    assert set_team.await_args.kwargs["seat_count"] == 5

    # Idempotency key must be time-bucketed, not a bare content-addressed
    # key — otherwise a later re-set to the same quantity within Stripe's
    # ~24h idempotency cache window would return the cached (stale) response
    # instead of actually re-applying the quantity, causing DDB/Stripe drift.
    idempotency_key = kwargs["idempotency_key"]
    prefix = "seat_sync:si_1:5:"
    assert idempotency_key.startswith(prefix)
    segments = idempotency_key.split(":")
    assert len(segments) == 4
    assert segments[3].isdigit()


@pytest.mark.asyncio
async def test_noop_when_count_unchanged(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "subscription_item_id": "si_1",
                "seat_count": 4,
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=4))
    set_team = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", set_team)

    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")

    assert result == 4
    item.modify.assert_not_called()
    set_team.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_without_active_subscription(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "canceled"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: False)
    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")
    assert result == 0
    item.modify.assert_not_called()


@pytest.mark.asyncio
async def test_noop_for_grandfathered_sub_without_item_id(monkeypatch):
    # Active sub but no subscription_item_id -> grandfathered non-seat sub.
    # Never touch it.
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "active", "seat_count": 0}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    count = AsyncMock()
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", count)
    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")
    assert result == 0
    item.modify.assert_not_called()
    count.assert_not_awaited()  # guarded out before the Clerk call
