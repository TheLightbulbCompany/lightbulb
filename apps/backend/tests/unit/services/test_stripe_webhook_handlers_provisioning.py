"""Event-driven provisioning dispatch from the Stripe subscription webhook.

``handle_subscription_state`` persists the Stripe-side subscription state and,
when the subscription becomes active/trialing, fires ``ensure_user_ready_for``
so the owner provisions off the event instead of waiting for the next
``/container/status`` poll. The gate inside ``ensure_user_ready`` no-ops if the
owner still isn't ready (e.g. no provider configured yet), and firing
concurrently with the poll yields exactly one company.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.stripe_webhook_handlers import handle_subscription_state


def _sub_event(*, status: str, metadata: dict) -> dict:
    return {
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": status,
                "trial_end": None,
                "metadata": metadata,
            }
        },
    }


def _patch_create_task():
    """Patch asyncio.create_task to close the dispatched coroutine (no
    "never awaited" warning); the dispatch is asserted via the mocked
    ensure_user_ready_for, not by running the coroutine."""
    return patch(
        "core.services.stripe_webhook_handlers.asyncio.create_task",
        side_effect=lambda coro: coro.close(),
    )


@pytest.mark.asyncio
async def test_subscription_active_dispatches_provisioning_personal(monkeypatch):
    """A personal owner's subscription going active fires ensure_user_ready_for
    (personal context) so they provision off the event."""
    repo = MagicMock()
    repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_x", "owner_type": "personal"})
    repo.set_subscription = AsyncMock()
    repo.set_provider_choice = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.billing_repo", repo)

    with (
        patch("core.services.stripe_webhook_handlers.ensure_user_ready_for", new=AsyncMock()) as mock_fire,
        _patch_create_task(),
    ):
        await handle_subscription_state(
            _sub_event(status="active", metadata={"owner_id": "user_x", "clerk_user_id": "user_x"}),
            MagicMock(),
        )

    mock_fire.assert_called_once_with(user_id="user_x", org_id=None, org_role=None)


@pytest.mark.asyncio
async def test_subscription_trialing_dispatches_provisioning_org(monkeypatch):
    """An org owner's subscription going trialing fires with org context so the
    admin path (create/join + container) runs."""
    repo = MagicMock()
    repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_x", "owner_type": "org"})
    repo.set_subscription = AsyncMock()
    repo.set_provider_choice = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.billing_repo", repo)

    with (
        patch("core.services.stripe_webhook_handlers.ensure_user_ready_for", new=AsyncMock()) as mock_fire,
        _patch_create_task(),
    ):
        await handle_subscription_state(
            _sub_event(status="trialing", metadata={"owner_id": "org_x", "clerk_user_id": "user_admin"}),
            MagicMock(),
        )

    mock_fire.assert_called_once_with(user_id="user_admin", org_id="org_x", org_role="org:admin")


@pytest.mark.asyncio
async def test_subscription_inactive_status_does_not_dispatch(monkeypatch):
    """A non-active status (e.g. canceled, past_due) must NOT fire provisioning."""
    repo = MagicMock()
    repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_x", "owner_type": "personal"})
    repo.set_subscription = AsyncMock()
    repo.set_provider_choice = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.billing_repo", repo)

    with (
        patch("core.services.stripe_webhook_handlers.ensure_user_ready_for", new=AsyncMock()) as mock_fire,
        patch("core.services.stripe_webhook_handlers.asyncio.create_task") as mock_task,
    ):
        await handle_subscription_state(
            _sub_event(status="canceled", metadata={"owner_id": "user_x", "clerk_user_id": "user_x"}),
            MagicMock(),
        )

    mock_fire.assert_not_called()
    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_subscription_active_without_clerk_user_id_does_not_dispatch(monkeypatch):
    """Legacy subscriptions lack clerk_user_id metadata — without it we can't
    build the member identity, so skip the dispatch (the poll is the backstop).
    Subscription state is still persisted."""
    repo = MagicMock()
    repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_x", "owner_type": "personal"})
    repo.set_subscription = AsyncMock()
    repo.set_provider_choice = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.billing_repo", repo)

    with (
        patch("core.services.stripe_webhook_handlers.ensure_user_ready_for", new=AsyncMock()) as mock_fire,
        patch("core.services.stripe_webhook_handlers.asyncio.create_task") as mock_task,
    ):
        await handle_subscription_state(
            _sub_event(status="active", metadata={"owner_id": "user_x"}),  # no clerk_user_id
            MagicMock(),
        )

    mock_fire.assert_not_called()
    mock_task.assert_not_called()
    repo.set_subscription.assert_awaited_once()  # state still persisted


@pytest.mark.asyncio
async def test_subscription_unresolved_owner_does_not_dispatch(monkeypatch):
    """If the owner can't be resolved, the handler returns early — no
    subscription write and no dispatch."""
    repo = MagicMock()
    repo.get_by_owner_id = AsyncMock(return_value=None)
    repo.list_by_stripe_customer_id = AsyncMock(return_value=[])
    repo.set_subscription = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.billing_repo", repo)

    with (
        patch("core.services.stripe_webhook_handlers.ensure_user_ready_for", new=AsyncMock()) as mock_fire,
        patch("core.services.stripe_webhook_handlers.asyncio.create_task") as mock_task,
    ):
        await handle_subscription_state(
            _sub_event(status="active", metadata={"owner_id": "user_x", "clerk_user_id": "user_x"}),
            MagicMock(),
        )

    mock_fire.assert_not_called()
    mock_task.assert_not_called()
    repo.set_subscription.assert_not_awaited()
