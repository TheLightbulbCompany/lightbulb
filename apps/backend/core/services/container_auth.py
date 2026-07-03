"""Container-side auth credential lifecycle for a RUNNING OpenClaw container.

Revocation delegates to the runtime port: ``Runtime.revoke_provider`` owns the
container resolution, the signed ``models.authLogout`` one-shot, and the
never-raise best-effort contract. This module keeps the stable public entry
point so callers (billing provider-switch, OAuth disconnect) never touch a
runtime adapter directly.

Credential push (``push_oauth_credential_to_container``) stays here: staging
the ChatGPT token onto EFS and clearing the default agent's auth row is
workspace plumbing, not a gateway RPC.
"""

import logging

from core.runtime import get_runtime_for_owner

logger = logging.getLogger(__name__)


async def revoke_provider_auth_in_container(owner_id: str, *, provider: str = "openai") -> bool:
    """Best-effort: clear ``provider``'s credential from the owner's RUNNING
    container, so disconnect / switch-away revokes the live token immediately
    without a restart.

    Returns ``True`` if the revoke succeeded, ``False`` otherwise. NEVER
    raises — when no container is running (cold) there is no live store to
    clear and the EFS seed-file delete is the authoritative cleanup; a
    transient failure is logged and self-heals on the next reprovision.
    """
    return await get_runtime_for_owner(owner_id).revoke_provider(owner_id, provider=provider)


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
    try:
        await pre_stage_auth_profile_store(user_id=owner_id, oauth_tokens=tokens)
        await clear_default_agent_auth_profile_row(user_id=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort (no workspace/container yet)
        logger.warning("push_oauth_credential: stage/clear failed for owner=%s: %s", owner_id, exc)
        return False
    logger.info("push_oauth_credential: staged fresh ChatGPT token", extra={"owner_id": owner_id})
    return True
