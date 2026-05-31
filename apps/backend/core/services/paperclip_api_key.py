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
   in Secrets Manager, we return it; otherwise we mint via Paperclip and
   store it. A missing-key reprovision recovers on the next
   ``write_user_configs`` run without any operator intervention.

Storage: AWS Secrets Manager at
``isol8/<env>/paperclip-agent-keys/<owner>/<paperclip_agent_id>``. The owner_id
is the container owner (Clerk user_id for personal, org_id for orgs). These keys
are re-mintable, so a reprovision (or the clean-slate reset) recovers them; the
debug teardown does not enumerate them — there is no per-agent index to sweep.

Single public function: ``ensure_paperclip_api_key``.
"""

from __future__ import annotations

import logging

import boto3
import httpx

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.secrets_manager import create_or_update_secret
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


def _secret_name(owner_id: str, paperclip_agent_id: str) -> str:
    """Secrets Manager name for the per-agent Paperclip key. Must match the
    resource scope granted to the backend task role in ``service-stack.ts``
    (``isol8/<env>/paperclip-agent-keys/*``)."""
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/paperclip-agent-keys/{owner_id}/{paperclip_agent_id}"


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
    """Read the stored key from Secrets Manager, or None if absent.

    Any read failure is treated as "no key" so the next pass re-mints; we
    never raise, so a transient Secrets Manager error degrades to a re-mint
    rather than blocking provision.
    """
    name = _secret_name(owner_id, paperclip_agent_id)

    def _get() -> str | None:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        try:
            return sm.get_secret_value(SecretId=name)["SecretString"]
        except sm.exceptions.ResourceNotFoundException:
            return None

    try:
        return await run_in_thread(_get)
    except Exception:
        logger.exception(
            "paperclip_api_key: Secrets Manager read failed owner=%s agent=%s",
            owner_id,
            paperclip_agent_id,
        )
        put_metric("paperclip_api_key.read.error")
        return None


async def _mint_and_store(
    *,
    owner_id: str,
    paperclip_agent_id: str,
    admin_client: PaperclipAdminClient,
) -> str | None:
    """Mint a fresh key via the Paperclip admin API and store it in Secrets
    Manager.

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

    name = _secret_name(owner_id, paperclip_agent_id)

    def _put() -> None:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        create_or_update_secret(sm, name, plaintext)

    try:
        await run_in_thread(_put)
    except Exception:
        logger.exception(
            "paperclip_api_key: failed to persist key owner=%s agent=%s — Paperclip "
            "still has the key but we lost it; next provision will mint a new one",
            owner_id,
            paperclip_agent_id,
        )
        put_metric("paperclip_api_key.store.error")
        return None

    put_metric("paperclip_api_key.mint.success")
    logger.info(
        "paperclip_api_key: minted + stored owner=%s agent=%s",
        owner_id,
        paperclip_agent_id,
    )
    return plaintext
