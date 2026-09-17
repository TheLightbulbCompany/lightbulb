"""Revoke a provider's auth credential from a RUNNING OpenClaw container.

On 6.6, the live credential lives in the container's per-agent SQLite auth
store (imported from ``credentials/oauth.json`` on first boot). Deleting the
EFS seed file does NOT clear that store, so a disconnect / switch-away must
also tell the running gateway to drop the credential — otherwise the already
imported refresh token stays usable until the container is reprovisioned.

The gateway exposes a built-in ``models.authLogout`` RPC (scope
``operator.admin``) that removes the provider's auth profiles across the owner
stores, invalidates the provider-auth cache, and aborts in-flight runs using
that provider (``stopReason: "auth-revoked"``) — a true revoke with no restart.

``operator.admin`` REQUIRES the signed operator-device handshake (the gateway
strips self-declared scopes from clients that present only the shared token —
see core/crypto/operator_device.py). The request itself goes over
``core.services.container_rpc.call``, the HTTP route to the container's baked
``lightbulb-channel`` plugin, like any other gateway RPC.
"""

import asyncio
import logging

from core.containers import get_ecs_manager
from core.services import container_rpc, runtime_transport

logger = logging.getLogger(__name__)

_WS_TIMEOUT = 10


async def _logout_provider_via_gateway(owner_id: str, provider: str) -> None:
    """``models.authLogout`` — drop ``provider``'s credential from the running
    gateway's live auth store. Raises on any failure (the caller treats
    failure as best-effort)."""
    await container_rpc.call(owner_id, "models.authLogout", {"provider": provider}, timeout_s=_WS_TIMEOUT)


async def revoke_provider_auth_in_container(owner_id: str, *, provider: str = "openai") -> bool:
    """Best-effort: clear ``provider``'s credential from the owner's RUNNING
    container via ``models.authLogout``, so disconnect / switch-away revokes the
    live token immediately without a restart.

    Returns ``True`` if the RPC succeeded, ``False`` otherwise. NEVER raises —
    when no container is running (cold) there is no live store to clear and the
    EFS seed-file delete is the authoritative cleanup; a transient RPC failure
    (or a pre-4.5 row with no paired operator device) is logged and self-heals
    on the next reprovision.

    On an AgentCore workspace there is no live gateway auth store and no
    ``models.authLogout`` on the bridge — the grant is a file, staged into the
    ROOT ``auth.json`` in S3 by ``hermes_profiles.stage_codex_auth``. Deleting
    it there IS the revoke, so this branch is authoritative rather than
    best-effort (task-14 row 3: without it a disconnect reported success while
    the agents kept using the revoked account).
    """
    if await runtime_transport.is_agentcore_owner(owner_id):
        from core.dynamodb import run_in_thread
        from core.services.hermes_profiles import unstage_codex_auth

        try:
            removed = await run_in_thread(unstage_codex_auth, owner_id)
        except Exception:  # noqa: BLE001 — never-raises contract: the whole disconnect chain
            # after this point (harness cleanup, clear_provider_choice) is best-effort, so a
            # transient S3 failure must not half-finish it. ERROR, not a warning: unlike the ECS
            # leg below there is no reprovision that self-heals this, and the grant is still live.
            logger.exception("revoke_provider_auth: could not unstage the AgentCore grant for %s", owner_id)
            return False
        logger.info(
            "revoke_provider_auth: unstaged the AgentCore ChatGPT grant",
            extra={"owner_id": owner_id, "provider": provider, "removed": removed},
        )
        return removed

    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("revoke_provider_auth: resolve container failed for %s: %s", owner_id, exc)
        return False

    if not container or not ip:
        return False  # cold container — nothing live to clear
    token = container.get("gateway_token")
    enc_seed = container.get("operator_priv_key_enc")
    if not token or not enc_seed:
        # Pre-4.5 row without a paired operator device — we can't sign the
        # operator.admin connect, so the live logout isn't possible. The EFS
        # seed delete + the eventual reprovision are the fallback cleanup.
        logger.warning(
            "revoke_provider_auth: missing gateway_token/operator seed for %s — "
            "skipping live logout (EFS delete + reprovision will clear)",
            owner_id,
        )
        return False

    try:
        await _logout_provider_via_gateway(owner_id, provider)
        logger.info(
            "revoked_provider_auth_in_container",
            extra={"owner_id": owner_id, "provider": provider},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("revoke_provider_auth: models.authLogout failed for %s: %s", owner_id, exc)
        return False


async def push_oauth_credential_to_container(*, owner_id: str, connector_id: str) -> bool:
    """Stage the connector's CURRENT ChatGPT token onto ``owner_id``'s container
    and force the running container to re-import it (clear the default agent's
    auth row so the store-row-absent import gate re-fires).

    Call ONLY on a genuine credential-change event — reconnect
    (``routers/oauth.py`` ``/poll``) or provider switch — where the token is
    freshly minted/unspent. NEVER call from reprovision / config-rewrite: those
    must not overwrite the container's live, rotated token (see
    ``ecs_manager._pre_stage_oauth_for_user``).

    Best-effort: returns ``True`` if a credential was staged, ``False`` if there
    are no active tokens or the owner has no container/workspace yet. NEVER
    raises.
    """
    from core.containers.workspace import (
        clear_default_agent_auth_profile_row,
        pre_stage_auth_profile_store,
    )
    from core.services import oauth_service

    try:
        tokens = await oauth_service.get_decrypted_tokens(user_id=connector_id, bound_to=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("push_oauth_credential: token read failed for owner=%s: %s", owner_id, exc)
        return False
    if not tokens:
        return False
    if await runtime_transport.is_agentcore_owner(owner_id):
        # No EFS stage, no clear-auth sentinel, no forced bounce: stage_codex_auth bumps
        # auth.revision, and the bridge's restore_root_auth compares it per turn (C3, fix round
        # 1 continuation) -- a warm microVM re-pulls the staged grant before its NEXT turn
        # rather than picking it up implicitly (Hermes itself only re-reads auth.json at process
        # start, not per credential resolution).
        from core.dynamodb import run_in_thread
        from core.services.hermes_profiles import stage_codex_auth

        await run_in_thread(stage_codex_auth, owner_id, tokens)
        logger.info("push_oauth_credential: staged fresh ChatGPT token (agentcore)", extra={"owner_id": owner_id})
        return True
    try:
        await pre_stage_auth_profile_store(user_id=owner_id, oauth_tokens=tokens)
        cleared = await clear_default_agent_auth_profile_row(user_id=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort (no workspace/container yet)
        logger.warning("push_oauth_credential: stage/clear failed for owner=%s: %s", owner_id, exc)
        return False
    if cleared == "restart_required":
        # local-sqlite layout: the auth row lives on container-local disk;
        # the clear left a boot sentinel on EFS, and only a task replacement
        # applies it. Bounce a RUNNING container asynchronously — this path
        # backs /oauth/poll, whose response must stay fast, and a cold or
        # never-booted container consumes the sentinel on its next boot anyway.
        asyncio.create_task(_restart_for_sentinel_apply(owner_id))
    logger.info("push_oauth_credential: staged fresh ChatGPT token", extra={"owner_id": owner_id})
    return True


async def _restart_for_sentinel_apply(owner_id: str) -> None:
    """Bounce a RUNNING container so its boot script applies the clear-auth
    sentinel and the staged oauth.json re-imports. No-op when the container is
    cold. Never raises — a failed bounce self-heals on the next boot."""
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not container or not ip:
            return
        await get_ecs_manager().force_redeploy_user_service(owner_id)
        logger.info("o6 auth-reimport: forced redeploy", extra={"owner_id": owner_id})
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("o6 auth-reimport restart failed for %s: %s", owner_id, exc)
