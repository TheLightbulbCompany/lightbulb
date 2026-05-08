"""Unit tests for the extracted background_provision helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.container_provision import background_provision


@pytest.mark.asyncio
async def test_background_provision_resolves_provider_and_dispatches():
    """background_provision reads provider_choice from billing_repo
    via _resolve_provider_choice and calls ecs.provision_user_container
    with the resolved values."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="svc-name")
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
    ):
        await background_provision("user_123", "user_123")
    fake_ecs.provision_user_container.assert_awaited_once_with(
        "user_123",
        provider_choice="bedrock_claude",
        byo_provider=None,
    )


@pytest.mark.asyncio
async def test_background_provision_swallows_exceptions():
    """Exceptions are logged, not raised. The helper is an
    asyncio.create_task target; raising would create unhandled
    task exceptions."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
    ):
        # Should NOT raise.
        await background_provision("user_123", "user_123")
