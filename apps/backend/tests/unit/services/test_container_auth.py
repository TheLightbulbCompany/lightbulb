"""Tests for revoke_provider_auth_in_container — the 6.6 live-store revoke.

Deleting the EFS seed (oauth.json) does not clear the credential the running
container already imported into its per-agent SQLite store; this helper calls
the gateway ``models.authLogout`` RPC to drop it without a restart. It is
strictly best-effort: never raises, returns False when there's no live store
to clear.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.container_auth as container_auth
from core.services.container_auth import revoke_provider_auth_in_container


def _ecs_manager(container, ip):
    m = MagicMock()
    m.resolve_running_container = AsyncMock(return_value=(container, ip))
    return m


@pytest.mark.asyncio
async def test_cold_container_skips_logout():
    with (
        patch("core.services.container_auth.get_ecs_manager", return_value=_ecs_manager(None, None)),
        patch("core.services.container_auth._logout_provider_via_gateway", new=AsyncMock()) as logout,
    ):
        assert await revoke_provider_auth_in_container("org_1") is False
        logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_container_calls_logout_for_openai():
    container = {"gateway_token": "tok", "operator_priv_key_enc": "enc-seed"}
    with (
        patch(
            "core.services.container_auth.get_ecs_manager",
            return_value=_ecs_manager(container, "10.0.0.5"),
        ),
        patch("core.services.container_auth._logout_provider_via_gateway", new=AsyncMock()) as logout,
    ):
        assert await revoke_provider_auth_in_container("org_1") is True
        logout.assert_awaited_once_with("org_1", "openai")


@pytest.mark.asyncio
async def test_logout_rpc_failure_is_best_effort():
    container = {"gateway_token": "tok", "operator_priv_key_enc": "enc-seed"}
    with (
        patch(
            "core.services.container_auth.get_ecs_manager",
            return_value=_ecs_manager(container, "10.0.0.5"),
        ),
        patch(
            "core.services.container_auth._logout_provider_via_gateway",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        assert await revoke_provider_auth_in_container("org_1") is False


@pytest.mark.asyncio
async def test_resolve_failure_is_best_effort():
    m = MagicMock()
    m.resolve_running_container = AsyncMock(side_effect=RuntimeError("aws down"))
    with patch("core.services.container_auth.get_ecs_manager", return_value=m):
        assert await revoke_provider_auth_in_container("org_1") is False


@pytest.mark.asyncio
async def test_missing_token_or_operator_seed_skips_logout():
    """A pre-4.5 row without a paired operator device (no seed), or one missing
    the gateway token, can't sign the operator.admin connect — skip the live
    logout (the EFS delete + reprovision are the fallback)."""
    for container in ({"operator_priv_key_enc": "enc-seed"}, {"gateway_token": "tok"}):
        with (
            patch(
                "core.services.container_auth.get_ecs_manager",
                return_value=_ecs_manager(container, "10.0.0.5"),
            ),
            patch("core.services.container_auth._logout_provider_via_gateway", new=AsyncMock()) as logout,
        ):
            assert await revoke_provider_auth_in_container("org_1") is False
            logout.assert_not_awaited()


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
