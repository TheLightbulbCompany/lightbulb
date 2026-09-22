"""Stage / revoke the owner's ChatGPT (Codex) OAuth grant for their workspace.

The grant is a FILE: the root ``auth.json`` under ``<owner>/hermes/`` in S3, written by
``hermes_profiles.stage_codex_auth`` and read by the bridge's ``restore_root_auth`` at the
start of each turn (it compares ``auth.revision``, so a warm microVM re-pulls a freshly staged
grant before its NEXT turn rather than picking it up implicitly -- Hermes itself only reads
``auth.json`` at process start). Deleting that file IS the revoke.

Both doors are therefore AUTHORITATIVE, not best-effort: a failure raises so the caller keeps
retry ownership. The OpenClaw shape this replaced was the opposite -- a live
``models.authLogout`` RPC into a running gateway's SQLite auth store, which silently no-op'd on
a cold container and let a disconnect report success while the agents kept using the revoked
account (task-14 row 3).
"""

import logging

from core.dynamodb import run_in_thread
from core.services.hermes_profiles import stage_codex_auth, unstage_codex_auth

logger = logging.getLogger(__name__)


async def revoke_provider_auth_in_container(owner_id: str, *, provider: str = "openai") -> bool:
    """Delete the owner's staged grant. ``True`` on success, including the idempotent retry
    where it was already absent; S3/config failures raise."""
    removed = await run_in_thread(unstage_codex_auth, owner_id)
    logger.info(
        "revoke_provider_auth: ChatGPT grant removed",
        extra={"owner_id": owner_id, "provider": provider, "removed": removed},
    )
    return True


async def push_oauth_credential_to_container(*, owner_id: str, connector_id: str, only_if_absent: bool = False) -> bool:
    """Stage the connector's CURRENT ChatGPT token for ``owner_id``.

    Call ONLY on a genuine credential-change event — reconnect (``routers/oauth.py``
    ``/poll``) or a provider switch — where the token is freshly minted and unspent. NEVER from
    a reprovision or config rewrite: those must not overwrite a token the runtime has since
    rotated. ``only_if_absent=True`` is the one exception: a reconcile may fill a MISSING grant,
    never replace one.

    ``False`` when there are no active tokens to stage. Never raises.
    """
    from core.services import oauth_service

    try:
        tokens = await oauth_service.get_decrypted_tokens(user_id=connector_id, bound_to=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("push_oauth_credential: token read failed for owner=%s: %s", owner_id, exc)
        return False
    if not tokens:
        return False
    await run_in_thread(stage_codex_auth, owner_id, tokens, only_if_absent=only_if_absent)
    logger.info("push_oauth_credential: staged fresh ChatGPT token", extra={"owner_id": owner_id})
    return True
