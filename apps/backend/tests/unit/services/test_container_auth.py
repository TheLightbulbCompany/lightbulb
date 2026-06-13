"""Tests for revoke_provider_auth_in_container — the 6.6 live-store revoke.

Deleting the EFS seed (oauth.json) does not clear the credential the running
container already imported into its per-agent SQLite store; this helper calls
the gateway ``models.authLogout`` RPC to drop it without a restart. It is
strictly best-effort: never raises, returns False when there's no live store
to clear.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        # Signed-handshake args: (ip, owner_id, enc_seed, token, provider).
        logout.assert_awaited_once_with("10.0.0.5", "org_1", "enc-seed", "tok", "openai")


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
