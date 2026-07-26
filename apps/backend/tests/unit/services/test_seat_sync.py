"""sync_team_seat_quantity — the single seat-quantity reconciler."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import billing_service, clerk_admin  # noqa: E402


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

    # No idempotency_key: a content-addressed or time-bucketed key would let
    # an oscillating member count (3->4->3->4 within one bucket) replay
    # Stripe's cached response instead of re-applying, causing DDB/Stripe
    # drift. An absolute `quantity=N` set is naturally idempotent on its
    # own — every distinct call must actually apply.
    assert "idempotency_key" not in kwargs


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


@pytest.mark.asyncio
async def test_seat_count_bills_pending_invitations(monkeypatch):
    """No free members: 1 admin + 3 unaccepted invites = 4 seats."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=1))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=3))

    assert await clerk_admin.get_organization_seat_count("org_1") == 4


@pytest.mark.asyncio
async def test_seat_count_unchanged_when_an_invite_is_accepted(monkeypatch):
    """Acceptance moves pending -> member; the total must not move."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=2))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=2))

    assert await clerk_admin.get_organization_seat_count("org_1") == 4


@pytest.mark.asyncio
async def test_seat_count_falls_back_to_members_when_invites_unreadable(monkeypatch):
    """Default (strict=False, used by /billing/account + checkout): an
    invitations-read failure floors to the member count rather than
    returning 0 — a members-only floor self-corrects upward on the next
    successful sync, whereas 0 would hide seat_count or 503 checkout on a
    transient Clerk blip."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=3))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=None))

    assert await clerk_admin.get_organization_seat_count("org_1") == 3


@pytest.mark.asyncio
async def test_seat_count_strict_returns_zero_when_invites_unreadable(monkeypatch):
    """strict=True (sync_team_seat_quantity's caller): an invitations-read
    failure must return 0, NOT the members-only floor — sync_team_seat_quantity
    treats 0 as a no-op, so an uncertain read leaves the Stripe quantity
    untouched instead of writing an absolute members-only quantity that would
    look like a real downgrade (proration credit) and get charged back on the
    next successful sync."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=3))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=None))

    assert await clerk_admin.get_organization_seat_count("org_1", strict=True) == 0


@pytest.mark.asyncio
async def test_seat_count_zero_when_member_read_fails(monkeypatch):
    """0 keeps sync_team_seat_quantity's existing no-op-on-Clerk-outage posture."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=0))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=5))

    assert await clerk_admin.get_organization_seat_count("org_1") == 0


@pytest.mark.asyncio
async def test_sync_does_not_downgrade_stripe_when_invites_unreadable(monkeypatch):
    """End-to-end regression for the strict-mode fix: 3 members + a stored
    seat_count=5 (from an earlier successful sync that saw 2 pending
    invites). The invitations read fails mid-sync (e.g. a 429 during a burst
    of container/status polls after a bulk invite). Must NOT write
    quantity=3 to Stripe — that would be a real downgrade (proration credit)
    immediately reversed by the next successful sync. Must no-op and
    preserve the stored 5."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
                "subscription_item_id": "si_1",
                "seat_count": 5,
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=3))
    monkeypatch.setattr("core.services.clerk_admin._pending_invitation_count", AsyncMock(return_value=None))
    set_team = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", set_team)

    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")

    assert result == 5
    item.modify.assert_not_called()
    set_team.assert_not_awaited()
