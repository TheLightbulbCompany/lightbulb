"""Per-agent Paperclip API key minting + storage.

Friday's container needs to call back into Paperclip via the
``@paperclipai/mcp-server`` MCP wrapper running inside OpenClaw. That MCP
authenticates with ``PAPERCLIP_API_KEY`` set in its env block.

Two design points worth knowing:

1. **Per-agent attribution.** Paperclip mints long-lived API keys scoped
   to a single agent via ``POST /api/agents/{agentId}/keys`` (see
   ``paperclip_admin_client.create_agent_api_key``). Using Friday's own
   key means every Paperclip action she takes is attributed to her in
   Paperclip's audit log — not to ``admin+paperclip@isol8.co`` the way
   the existing admin-API call sites are.

2. **Self-healing.** This helper is idempotent: if the key already exists
   in ``api_keys``, we return the plaintext from there; otherwise we
   mint via Paperclip, encrypt, and store. That means a missing-key
   reprovision recovers on the next ``write_user_configs`` run without
   any operator intervention.

Storage: existing ``api_keys`` DDB table, keyed by
``(owner_id, tool_id="paperclip-agent-key:<paperclip_uuid>")``. Fernet
encryption via ``core.encryption.encrypt``/``decrypt``. The owner_id is
the container owner (Clerk user_id for personal, org_id for orgs) so
container teardown via ``api_key_repo.delete_all_for_owner`` cleans up
the keys alongside.

Single public function: ``ensure_paperclip_api_key``.
"""

from __future__ import annotations

import logging

import httpx

from core.encryption import decrypt, encrypt
from core.observability.metrics import put_metric
from core.repositories import api_key_repo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


def _tool_id(paperclip_agent_id: str) -> str:
    """Namespaced ``tool_id`` for the api_keys row — keeps these out of
    the BYOK (Anthropic/OpenAI) namespace so ``list_keys`` consumers
    can filter cleanly."""
    return f"paperclip-agent-key:{paperclip_agent_id}"


async def ensure_paperclip_api_key(
    *,
    owner_id: str,
    paperclip_agent_id: str,
    admin_client: PaperclipAdminClient,
) -> str | None:
    """Return the plaintext Paperclip API key for ``paperclip_agent_id``,
    minting + storing it on first request.

    Returns None on any failure — the caller (``write_openclaw_config``)
    treats absent keys as "skip the MCP server block this provision; try
    again next time." Failures are logged + metric'd, never raised: a
    Paperclip outage must not block container provision.
    """
    existing = await _read_existing(owner_id=owner_id, paperclip_agent_id=paperclip_agent_id)
    if existing is not None:
        return existing

    return await _mint_and_store(
        owner_id=owner_id,
        paperclip_agent_id=paperclip_agent_id,
        admin_client=admin_client,
    )


async def _read_existing(
    *,
    owner_id: str,
    paperclip_agent_id: str,
) -> str | None:
    """Read + decrypt a stored key, or return None if absent / corrupt.

    Corrupt cells (decrypt failure) are treated as "no key" so the next
    pass re-mints. We do not delete the row — if Paperclip's key endpoint
    is the source of the corruption, deleting on the read path could
    nuke a perfectly good key that just needs the right Fernet token.
    """
    try:
        row = await api_key_repo.get_key(owner_id, _tool_id(paperclip_agent_id))
    except Exception:
        logger.exception(
            "paperclip_api_key: api_key_repo.get_key failed owner=%s agent=%s",
            owner_id,
            paperclip_agent_id,
        )
        put_metric("paperclip_api_key.read.error")
        return None

    if not row:
        return None

    encrypted = row.get("encrypted_key")
    if not encrypted:
        return None

    try:
        return decrypt(encrypted)
    except Exception:
        logger.exception(
            "paperclip_api_key: decrypt failed for owner=%s agent=%s — will re-mint",
            owner_id,
            paperclip_agent_id,
        )
        put_metric("paperclip_api_key.decrypt.error")
        return None


async def _mint_and_store(
    *,
    owner_id: str,
    paperclip_agent_id: str,
    admin_client: PaperclipAdminClient,
) -> str | None:
    """Mint a fresh key via the Paperclip admin API, encrypt, store.

    Returns the plaintext on success, None on any failure. Failures are
    swallowed — provision continues without an MCP-server block this run.
    """
    try:
        cookie = await get_admin_session_cookie(admin_client._http)
    except RuntimeError as exc:
        logger.warning(
            "paperclip_api_key: admin session unavailable owner=%s: %s",
            owner_id,
            exc,
        )
        put_metric("paperclip_api_key.admin_session.error")
        return None

    try:
        result = await admin_client.create_agent_api_key(
            session_cookie=cookie,
            agent_id=paperclip_agent_id,
            name="isol8-mcp-server",
            idempotency_key=f"{owner_id}:mcp-key:{paperclip_agent_id}",
        )
    except (PaperclipApiError, httpx.HTTPError) as exc:
        logger.warning(
            "paperclip_api_key: create_agent_api_key failed owner=%s agent=%s: %s",
            owner_id,
            paperclip_agent_id,
            exc,
        )
        put_metric("paperclip_api_key.mint.error")
        return None

    plaintext = result.get("token") if isinstance(result, dict) else None
    if not isinstance(plaintext, str) or not plaintext:
        logger.warning(
            "paperclip_api_key: paperclip response missing token owner=%s agent=%s response_keys=%s",
            owner_id,
            paperclip_agent_id,
            list(result.keys()) if isinstance(result, dict) else type(result).__name__,
        )
        put_metric("paperclip_api_key.mint.missing_token")
        return None

    try:
        encrypted = encrypt(plaintext)
        await api_key_repo.set_key(
            user_id=owner_id,
            tool_id=_tool_id(paperclip_agent_id),
            encrypted_key=encrypted,
        )
    except Exception:
        logger.exception(
            "paperclip_api_key: failed to persist key owner=%s agent=%s — Paperclip "
            "still has the key but we lost it; next provision will mint a new one",
            owner_id,
            paperclip_agent_id,
        )
        put_metric("paperclip_api_key.store.error")
        # The key exists in Paperclip but we can't read it from DDB. Returning
        # None means this provision skips the MCP server block; the next
        # provision will mint a fresh key.
        return None

    put_metric("paperclip_api_key.mint.success")
    logger.info(
        "paperclip_api_key: minted + stored owner=%s agent=%s",
        owner_id,
        paperclip_agent_id,
    )
    return plaintext
