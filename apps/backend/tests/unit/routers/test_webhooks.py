"""Unit tests for organization-membership webhook record keeping + Paperclip join."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_membership_created_renews_member_lifecycle():
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }

    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()) as renew,
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
    ):
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)

    renew.assert_awaited_once_with("org_xyz", "user_member", None)


@pytest.mark.asyncio
async def test_membership_created_ignores_missing_email():
    payload = {"organization": {"id": "org_xyz"}, "public_user_data": {"user_id": "user_member"}}

    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()) as renew,
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()) as join,
    ):
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)

    renew.assert_not_awaited()
    join.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_created_invokes_paperclip_join():
    """The webhook must not only record the event — it drives the same
    join-only, idempotent helper ``ensure_user_ready`` calls from the
    member's own poll, so a member who never polls still lands in Teams."""
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }

    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()) as join,
    ):
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)

    join.assert_awaited_once_with(
        org_id="org_xyz",
        member_user_id="user_member",
        member_email="member@example.com",
    )


@pytest.mark.asyncio
async def test_membership_created_join_failure_is_logged_and_metered_not_raised():
    """A blown-up Paperclip join must never fail the webhook response — Clerk
    retries on any non-2xx, and the lifecycle bookkeeping already ran and
    must still be acknowledged. The member's own next poll retries the join."""
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }

    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()),
        patch(
            "core.services.user_provision._ensure_paperclip_for_member",
            new=AsyncMock(side_effect=RuntimeError("paperclip is down")),
        ),
        patch("routers.webhooks.put_metric") as put_metric,
    ):
        from routers.webhooks import _handle_organization_membership_created

        # Must not raise.
        await _handle_organization_membership_created(payload)

    put_metric.assert_any_call("paperclip.member_add", dimensions={"outcome": "webhook_error"})
