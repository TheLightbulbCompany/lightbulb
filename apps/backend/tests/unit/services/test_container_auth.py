"""Stage / revoke the owner's ChatGPT (Codex) grant — the ROOT ``auth.json`` in S3.

WHAT WENT WITH THE ECS RUNTIME. Revocation used to be a live ``models.authLogout`` RPC into a
running gateway's SQLite auth store; the bridge does not serve that method, so on a migrated
workspace the helper quietly returned False, the grant survived in the root ``auth.json``, and
the agents kept using the account the owner had just disconnected (M6 task-14 row 3). Staging
used to write an EFS auth-profile store, drop a clear-auth sentinel and force a redeploy.

Both doors are now one S3 write/delete each, and both are AUTHORITATIVE rather than
best-effort: a failure raises so the caller keeps retry ownership. With the gateway leg gone the
module reads no container row and schedules no bounce, so the tests over the row read, the
gateway RPC, the sentinel and the restart went with them.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import container_auth
from core.services.container_auth import revoke_provider_auth_in_container

_TOKENS = {"access_token": "a", "refresh_token": "r"}


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_unstages_the_root_grant():
    with patch("core.services.container_auth.unstage_codex_auth", return_value=True) as unstage:
        assert await revoke_provider_auth_in_container("org_1") is True
    unstage.assert_called_once_with("org_1")


@pytest.mark.asyncio
async def test_revoke_is_idempotent_when_there_was_no_grant():
    """A retried disconnect must report success, not a failure the caller keeps retrying."""
    with patch("core.services.container_auth.unstage_codex_auth", return_value=False) as unstage:
        assert await revoke_provider_auth_in_container("org_1") is True
    unstage.assert_called_once_with("org_1")


@pytest.mark.asyncio
async def test_revoke_propagates_an_authoritative_store_failure():
    """The whole point of the rewrite: a revoke that could not prove it happened must NOT
    report success. The old RPC shape swallowed this and let a disconnect look complete while
    the agents kept using the revoked account."""
    with patch("core.services.container_auth.unstage_codex_auth", side_effect=RuntimeError("s3 down")):
        with pytest.raises(RuntimeError, match="s3 down"):
            await revoke_provider_auth_in_container("org_1")


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_credential_stages_the_root_auth_json():
    with (
        patch("core.services.oauth_service.get_token_row", new=AsyncMock(return_value={"tokens": _TOKENS})),
        patch("core.services.container_auth.stage_codex_auth") as stage,
    ):
        assert await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c") is True
    stage.assert_called_once_with("org_1", _TOKENS, only_if_absent=False)


@pytest.mark.asyncio
async def test_push_credential_noops_when_there_are_no_tokens():
    with (
        patch("core.services.oauth_service.get_token_row", new=AsyncMock(return_value=None)),
        patch("core.services.container_auth.stage_codex_auth") as stage,
    ):
        assert await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c") is False
    stage.assert_not_called()


@pytest.mark.asyncio
async def test_push_credential_reports_false_when_the_token_read_fails():
    """Best-effort on the READ only: the caller is a reconnect poll that will come round again,
    and a raise here would surface as a failed connect for a token we simply could not fetch."""
    with (
        patch(
            "core.services.oauth_service.get_token_row",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch("core.services.container_auth.stage_codex_auth") as stage,
    ):
        assert await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c") is False
    stage.assert_not_called()


@pytest.mark.asyncio
async def test_push_credential_propagates_a_stage_failure():
    """Unlike the read, the WRITE is authoritative: a staged grant nobody can prove landed must
    not report success -- the owner would see "connected" with no credential in the profile."""
    with (
        patch("core.services.oauth_service.get_token_row", new=AsyncMock(return_value={"tokens": _TOKENS})),
        patch("core.services.container_auth.stage_codex_auth", side_effect=RuntimeError("s3 down")),
    ):
        with pytest.raises(RuntimeError, match="s3 down"):
            await container_auth.push_oauth_credential_to_container(owner_id="org_1", connector_id="user_c")


@pytest.mark.asyncio
async def test_the_module_reaches_no_runtime_and_schedules_no_bounce():
    """Hermes re-reads the root ``auth.json`` at the start of each turn (the bridge compares
    ``auth.revision``), so a staged grant needs no restart -- and there is no gateway to RPC."""
    for gone in ("get_ecs_manager", "runtime_transport", "container_rpc", "_logout_provider_via_gateway"):
        assert not hasattr(container_auth, gone), gone


# ---------------------------------------------------------------------------
# hook-token guard
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, authorization: str | None):
        self.headers = {} if authorization is None else {"Authorization": authorization}


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer \u00fcn\u00efcode"])
async def test_hook_token_rejects_missing_wrong_and_non_ascii_bearer_with_401(authorization):
    """Non-ASCII must be a clean 401, not a 500: compare_digest raises TypeError on non-ASCII str
    (seen live 2026-08-31 when a container agent's curl mangled the token)."""
    from fastapi import HTTPException

    row = {"owner_id": "org_a", "hook_token": "tok"}
    with patch.object(container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc:
            await container_auth._require_hook_token("org_a", _Req(authorization))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_hook_token_returns_the_container_row_on_match():
    row = {"owner_id": "org_a", "hook_token": "tok"}
    with patch.object(container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        assert await container_auth._require_hook_token("org_a", _Req("Bearer tok")) is row
