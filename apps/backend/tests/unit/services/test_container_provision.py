"""Unit tests for the extracted background_provision helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager
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


async def test_pre_stage_oauth_reads_connector_writes_owner():
    """The token READ key (connector / workspace owner) is decoupled from
    the EFS WRITE key (container owner / org_id). For an org, tokens live
    under the owner's Clerk user_id but the file must land on the org's
    EFS path — so a single ``user_id`` arg cannot serve both."""
    mgr = object.__new__(EcsManager)  # no AWS clients needed for this path
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ) as read,
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(),
        ) as write,
    ):
        await EcsManager._pre_stage_oauth_for_user(mgr, "org_1", token_owner_id="user_a")
    read.assert_awaited_once_with(user_id="user_a")
    write.assert_awaited_once()
    assert write.call_args.kwargs["user_id"] == "org_1"
