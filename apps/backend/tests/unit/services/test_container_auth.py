"""Tests for revoke_provider_auth_in_container — the 6.6 live-store revoke.

Deleting the EFS seed (oauth.json) does not clear the credential the running
container already imported into its per-agent SQLite store; this helper calls
the gateway ``models.authLogout`` RPC to drop it without a restart. It is
strictly best-effort: never raises, returns False when there's no live store
to clear.
"""

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
        # The shared signed one-shot re-loads the seed from the container row,
        # so the wrapper takes (ip, owner_id, token, provider).
        logout.assert_awaited_once_with("10.0.0.5", "org_1", "tok", "openai")


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
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is True
    stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "a", "refresh_token": "r"})
    clear.assert_awaited_once_with(user_id="org_1")


async def test_push_credential_noop_when_no_tokens():
    with (
        patch("core.services.oauth_service.get_decrypted_tokens", new=AsyncMock(return_value=None)),
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
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(side_effect=RuntimeError("no workspace yet")),
        ),
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")
    assert result is False  # never raises
    clear.assert_not_called()
