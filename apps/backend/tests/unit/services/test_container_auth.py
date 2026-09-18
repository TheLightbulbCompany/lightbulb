"""Tests for revoke_provider_auth_in_container — the AgentCore live-store revoke.

Every owner is AgentCore now (``runtime_transport.host_for`` always returns
``"agentcore"``), so the ECS-era live gateway ``models.authLogout`` RPC leg is
dead: revocation always unstages the ROOT ``auth.json`` grant in S3
(``hermes_profiles.unstage_codex_auth``), and that path is authoritative
rather than best-effort — see the function's own docstring.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import core.services.container_auth as container_auth
from core.services.container_auth import revoke_provider_auth_in_container


@pytest.mark.asyncio
async def test_agentcore_revoke_unstages_the_root_grant_instead_of_an_rpc():
    """M6 task-14 row 3: ``models.authLogout`` does not exist on the bridge, so this whole helper
    quietly returned False on an AgentCore workspace — the grant survived in the ROOT auth.json
    and the agents kept using the account the owner had just disconnected. Deleting it there IS
    the revoke, so the gateway path must not be touched at all."""
    with (
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"runtime_host": "agentcore"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch("core.services.hermes_profiles.unstage_codex_auth", return_value=True) as unstage,
        patch("core.services.container_auth.get_ecs_manager") as ecs,
        patch("core.services.container_auth._logout_provider_via_gateway", new=AsyncMock()) as logout,
    ):
        assert await revoke_provider_auth_in_container("org_1") is True
    unstage.assert_called_once_with("org_1")
    ecs.assert_not_called()
    logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_agentcore_revoke_uses_the_actual_container_row_consistently():
    """A failed runtime-host lookup must not default an AgentCore owner to the ECS best-effort
    path. Revocation makes the security-sensitive routing decision from the actual row."""
    read = AsyncMock(return_value={"runtime_host": "agentcore"})
    with (
        patch("core.repositories.container_repo.get_by_owner_id", read),
        patch(
            "core.services.container_auth.runtime_transport.is_agentcore_owner",
            new=AsyncMock(side_effect=AssertionError("the swallowing predicate is not authoritative")),
        ),
        patch("core.services.hermes_profiles.unstage_codex_auth", return_value=True),
    ):
        assert await revoke_provider_auth_in_container("org_1") is True
    read.assert_awaited_once_with("org_1", consistent=True)


@pytest.mark.asyncio
async def test_agentcore_revoke_is_idempotent_when_there_was_no_grant():
    with (
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"runtime_host": "agentcore"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch("core.services.hermes_profiles.unstage_codex_auth", return_value=False),
    ):
        assert await revoke_provider_auth_in_container("org_1") is True


@pytest.mark.asyncio
async def test_agentcore_revoke_propagates_authoritative_store_failure():
    """A failed S3 delete must leave the caller's retry ownership intact, not degrade to the ECS
    best-effort contract and let disconnect report success."""
    with (
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"runtime_host": "agentcore"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch("core.services.hermes_profiles.unstage_codex_auth", side_effect=RuntimeError("s3 down")),
    ):
        with pytest.raises(RuntimeError, match="s3 down"):
            await revoke_provider_auth_in_container("org_1")


@pytest.mark.asyncio
async def test_revoke_propagates_container_row_read_failure():
    with (
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
    ):
        with pytest.raises(RuntimeError, match="ddb down"):
            await revoke_provider_auth_in_container("org_1")


async def test_push_credential_stages_and_clears_when_tokens_present():
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is True
    stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "a", "refresh_token": "r"})
    clear.assert_awaited_once_with(user_id="org_1")


async def test_push_credential_stages_the_root_auth_json_for_an_agentcore_owner_no_efs():
    """Task 15: an AgentCore owner has no EFS to stage, no clear-auth-sentinel, no forced
    redeploy — the equivalent is hermes_profiles.stage_codex_auth writing the ROOT auth.json,
    which Hermes re-reads at credential resolution time (no bounce needed)."""
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=True)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
        patch("core.services.hermes_profiles.stage_codex_auth") as codex_stage,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is True
    codex_stage.assert_called_once_with("org_1", {"access_token": "a", "refresh_token": "r"})
    stage.assert_not_awaited()  # the EFS path is never reached
    clear.assert_not_awaited()


async def test_push_credential_noop_when_no_tokens():
    with (
        patch("core.services.oauth_service.get_decrypted_tokens", new=AsyncMock(return_value=None)),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is False
    stage.assert_not_called()
    clear.assert_not_called()


async def test_push_credential_best_effort_when_stage_raises():
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(side_effect=RuntimeError("no workspace yet")),
        ),
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is False  # never raises
    clear.assert_not_called()


async def test_push_credential_restart_required_schedules_bounce():
    """O6 layout: the clear reports restart_required -> an async bounce is
    scheduled (fire-and-forget; /oauth/poll response must not block on it)."""
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()),
        patch(
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(return_value="restart_required"),
        ),
        patch("core.services.container_auth._restart_for_sentinel_apply", new=AsyncMock()) as bounce,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
        await asyncio.sleep(0)
    assert result is True
    bounce.assert_awaited_once_with("org_1")


async def test_push_credential_plain_clear_does_not_bounce():
    """Old layout (clear returned a bool): no bounce is ever scheduled."""
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.services.container_auth.runtime_transport.is_agentcore_owner", new=AsyncMock(return_value=False)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()),
        patch(
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(return_value=True),
        ),
        patch("core.services.container_auth._restart_for_sentinel_apply", new=AsyncMock()) as bounce,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
        await asyncio.sleep(0)
    assert result is True
    bounce.assert_not_awaited()


async def test_restart_for_sentinel_apply_only_bounces_running():
    """The bounce helper redeploys ONLY a running container; a cold one
    consumes the sentinel on its next boot anyway. Never raises."""
    with patch("core.services.container_auth.get_ecs_manager") as get_mgr:
        mgr = get_mgr.return_value
        mgr.resolve_running_container = AsyncMock(return_value=(None, None))
        mgr.force_redeploy_user_service = AsyncMock()
        await container_auth._restart_for_sentinel_apply("org_1")
        mgr.force_redeploy_user_service.assert_not_called()

        mgr.resolve_running_container = AsyncMock(return_value=({"service_name": "s"}, "10.0.0.1"))
        await container_auth._restart_for_sentinel_apply("org_1")
        mgr.force_redeploy_user_service.assert_awaited_once_with("org_1")


async def test_restart_for_sentinel_apply_swallows_errors():
    with patch("core.services.container_auth.get_ecs_manager") as get_mgr:
        get_mgr.return_value.resolve_running_container = AsyncMock(side_effect=RuntimeError("ecs down"))
        await container_auth._restart_for_sentinel_apply("org_1")  # must not raise
