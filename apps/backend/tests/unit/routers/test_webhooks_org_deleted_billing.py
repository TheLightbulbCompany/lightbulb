"""organization.deleted must cancel the org's Stripe subscription (Fix B).

Before this fix, ``_handle_organization_deleted`` only disabled the
org's Paperclip rows -- it never touched Stripe, so a deleted
Clerk org kept billing its workspace subscription forever.

The fix reuses the same Stripe-cancel path as the admin "cancel
subscription" action (``billing_service.cancel_subscription_for_owner``),
keyed on the org id as owner_id (org billing rows are partitioned on
org_id, not per-member user_id). Cancelling at Stripe (rather than just
clearing the DDB row) fires the inbound ``customer.subscription.deleted``
webhook, which handles container teardown -- closing the loop.

The whole thing must be best-effort: a Stripe error must never break the
org.deleted webhook, which has to keep returning 200 to Clerk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patch_provisioning(monkeypatch):
    """``disable_workspace`` is the one workspace write ``_handle_organization_deleted`` makes."""
    provisioning = MagicMock(disable_workspace=AsyncMock())
    monkeypatch.setattr("core.services.workspace_provisioning.disable_workspace", provisioning.disable_workspace)
    monkeypatch.setattr("core.services.workspace_join.revoke_join_link", AsyncMock())
    monkeypatch.setattr("core.repositories.openrouter_key_repo.mark_retiring", AsyncMock(return_value=False))
    return provisioning


@pytest.mark.asyncio
async def test_organization_deleted_cancels_active_stripe_subscription(monkeypatch):
    """Org with a live subscription -> Stripe cancel called with its sub id,
    and the billing row is cleared (subscription_id=None, status=canceled)."""
    from routers import webhooks

    provisioning = _patch_provisioning(monkeypatch)

    billing_row = {"owner_id": "org_acme", "stripe_subscription_id": "sub_123"}
    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=billing_row),
        ),
        patch(
            "core.services.billing_service.billing_repo.set_subscription",
            new=AsyncMock(),
        ) as mock_set_sub,
        patch("core.services.billing_service.stripe.Subscription.delete") as mock_delete,
    ):
        await webhooks._handle_organization_deleted({"id": "org_acme"})

    mock_delete.assert_called_once()
    call_args, call_kwargs = mock_delete.call_args
    assert call_args[0] == "sub_123"
    assert call_kwargs.get("idempotency_key") == "sub_cancel:sub_123"

    mock_set_sub.assert_awaited_once()
    set_sub_kwargs = mock_set_sub.call_args.kwargs
    assert set_sub_kwargs["owner_id"] == "org_acme"
    assert set_sub_kwargs["subscription_id"] is None
    assert set_sub_kwargs["status"] == "canceled"
    # The workspace's ONE Paperclip row is disabled once, by org id.
    provisioning.disable_workspace.assert_awaited_once_with("org_acme")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_choice", ["openrouter_managed", "byo_key"])
async def test_organization_deleted_retires_managed_key_even_if_stripe_cancel_fails(monkeypatch, provider_choice):
    from routers import webhooks

    _patch_provisioning(monkeypatch)
    retire = AsyncMock(return_value=True)
    row = {"owner_id": "org_acme", "stripe_subscription_id": "sub_123", "provider_choice": provider_choice}
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
        patch("core.repositories.openrouter_key_repo.mark_retiring", retire),
        patch("core.services.billing_service.stripe.Subscription.delete", side_effect=RuntimeError("stripe down")),
    ):
        await webhooks._handle_organization_deleted({"id": "org_acme"})
    retire.assert_awaited_once_with("org_acme")


@pytest.mark.asyncio
async def test_organization_deleted_no_billing_row_no_cancel(monkeypatch):
    """Org with no billing account at all -> no Stripe call, no raise."""
    from routers import webhooks

    _patch_provisioning(monkeypatch)

    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.billing_service.stripe.Subscription.delete") as mock_delete,
    ):
        await webhooks._handle_organization_deleted({"id": "org_no_billing"})

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_organization_deleted_stripe_error_is_swallowed(monkeypatch):
    """A Stripe cancel failure must not propagate -- the webhook has to
    keep returning 200 to Clerk even when Stripe is down/errors."""
    from routers import webhooks

    _patch_provisioning(monkeypatch)

    billing_row = {"owner_id": "org_broken", "stripe_subscription_id": "sub_999"}
    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=billing_row),
        ),
        patch(
            "core.services.billing_service.stripe.Subscription.delete",
            side_effect=RuntimeError("stripe down"),
        ),
    ):
        # Must not raise.
        await webhooks._handle_organization_deleted({"id": "org_broken"})


@pytest.mark.asyncio
async def test_organization_deleted_workspace_disable_failure_does_not_block_cancel(monkeypatch):
    """A DynamoDB failure in ``disable_workspace`` must neither prevent the
    Stripe cancel (which runs first, in its own try/except) nor escape the
    handler -- the webhook has to keep returning 200 to Clerk."""
    from routers import webhooks

    provisioning = _patch_provisioning(monkeypatch)
    provisioning.disable_workspace.side_effect = RuntimeError("ddb throttled")

    billing_row = {"owner_id": "org_sweep_fail", "stripe_subscription_id": "sub_456"}
    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=billing_row),
        ),
        patch(
            "core.services.billing_service.billing_repo.set_subscription",
            new=AsyncMock(),
        ),
        patch("core.services.billing_service.stripe.Subscription.delete") as mock_delete,
    ):
        await webhooks._handle_organization_deleted({"id": "org_sweep_fail"})

    provisioning.disable_workspace.assert_awaited_once_with("org_sweep_fail")
    mock_delete.assert_called_once()
    call_args, _call_kwargs = mock_delete.call_args
    assert call_args[0] == "sub_456"
