"""Chat-initiated Paperclip agent lifecycle.

When /chat intercepts an ``agents.create`` or ``agents.delete`` RPC it
needs to mirror the operation against Paperclip — otherwise the next
container reprovision rebuilds ``openclaw.json`` from the live Paperclip
list and resurrects the supposedly-deleted agent (or fails to find a
freshly-created one).

These helpers were previously co-located in ``routers/websocket_chat``
where they pulled the router back across the routers→services boundary
and re-invented the ``httpx.AsyncClient`` + admin-cookie boilerplate that
``core/services/paperclip_admin_session`` already owns.
"""

from __future__ import annotations

import logging

import httpx

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import service_token
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_meta import PaperclipMeta, resolve_create_agent_fields
from core.services.paperclip_openclaw_pairing import _resolve_company_id_for_owner
from core.services.paperclip_provisioning import _ws_gateway_url

logger = logging.getLogger(__name__)


async def create_paperclip_for_chat(
    *,
    creator_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
    model: str | None,
    meta: PaperclipMeta | None = None,
) -> dict:
    """Create a Paperclip agent invoked from /chat WS RPC interception.

    ``creator_user_id``: the authenticated member triggering the create;
        baked into adapterConfig.sessionKey as the manual-wake fallback.
    ``owner_id``: container owner (org_id for orgs, user_id for personal);
        used to look up the shared Paperclip company.

    The ``model`` parameter is accepted but not currently forwarded to
    Paperclip's ``create_agent`` — Paperclip's adapter selects the model
    server-side from the agent's role + company defaults. We thread the
    field through anyway so future schema additions don't require a
    second signature change.

    ``meta``: optional :class:`PaperclipMeta` carrying role/title/
        capabilities/icon parsed off ``params.paperclip`` at the chat
        boundary. When supplied, threads those fields into
        ``create_agent``. See :mod:`core.services.paperclip_meta`.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        raise RuntimeError(
            f"owner {owner_id} has no Paperclip company — agent create blocked. Re-run org provisioning."
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        # Mint the service token with owner_id (NOT creator_user_id). The
        # WS authorizer puts service tokens into the connection context with
        # ``orgId=""`` and ``userId=<sub>`` — minting with the member id
        # would route subsequent Paperclip wakes as the member's PERSONAL
        # owner instead of the org's shared owner, and the agent could
        # never reach the container it was just paired into.
        # ``user_id=creator_user_id`` is still correct: that field is the
        # adapterConfig.sessionKey fallback (per-creator manual-wake
        # session key), not the auth principal.
        adapter_config = synthesize_openclaw_adapter(
            gateway_url=_ws_gateway_url(settings.ENVIRONMENT or ""),
            service_token=service_token.mint(owner_id),
            user_id=creator_user_id,
        )
        # No idempotency_key: keying on (creator_user_id, name) returns a
        # stale (potentially deleted) record if the same friendly name is
        # reused within Paperclip's 24h dedupe window — same regression
        # class as memory feedback_stripe_idempotency_with_stable_keys.
        # Paperclip's create_agent treats idempotency_key as Optional, so
        # dropping it is safe.
        fields = resolve_create_agent_fields(role=role, meta=meta)
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            adapter_type=OPENCLAW_GATEWAY_TYPE,
            adapter_config=adapter_config,
            session_cookie=admin_cookie,
            **fields,
        )
    finally:
        await http.aclose()


async def delete_paperclip_for_chat(
    *,
    owner_id: str,
    agent_id: str,
) -> None:
    """Delete a Paperclip agent triggered by a /chat ``agents.delete`` RPC.

    Cascade-delete partner to ``create_paperclip_for_chat``: container
    provision rebuilds ``openclaw.json`` from
    ``fetch_paperclip_agents_for_owner``, so a Paperclip row that
    survives the OpenClaw delete will resurrect the agent on the next
    reprovision.

    Idempotent: ``PaperclipAdminClient.delete_agent`` swallows 404 from
    Paperclip (treats already-gone as success), so legacy 'main' agents
    that never existed in Paperclip and double-deletes are safe.

    Looks up the Paperclip company under the OWNER (org_id for orgs,
    user_id for personal), mirroring the create path.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        # No Paperclip company — owner was never provisioned through the
        # paired path, or the row was already cleaned. Nothing to delete.
        logger.info(
            "delete_paperclip_for_chat: owner %s has no Paperclip company — skip",
            owner_id,
        )
        return

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        await admin_client.delete_agent(
            session_cookie=admin_cookie,
            agent_id=agent_id,
        )
    finally:
        await http.aclose()


async def create_paperclip_for_marketplace(
    *,
    buyer_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
    meta: PaperclipMeta | None = None,
) -> dict:
    """Create a Paperclip agent for a marketplace install.

    Mirror of ``create_paperclip_for_chat`` for the storefront
    ``/marketplace/listings/{slug}/deploy`` flow. Returns the Paperclip
    agent dict (caller threads ``["id"]`` into ``catalog_service.
    deploy_from_artifact`` as ``new_agent_id`` so Paperclip and
    OpenClaw share the same UUID).

    ``buyer_user_id``: the authenticated buyer triggering Hire (used as
        adapterConfig.sessionKey, the manual-wake fallback).
    ``owner_id``: container owner (org_id for orgs, user_id for personal);
        used to look up the shared Paperclip company AND mint the service
        token. Same owner-vs-member split as the chat path.
    ``name``: agent display name — taken from the slice's agent entry,
        falls back to the listing's slug.
    ``role``: Paperclip role string (e.g. "general"). Defaults to "general"
        when None.
    ``meta``: optional :class:`PaperclipMeta` carrying role/title/
        capabilities/icon derived from the listing's slice. When supplied,
        threads those fields into ``create_agent``. See
        :mod:`core.services.paperclip_meta`.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        raise RuntimeError(
            f"owner {owner_id} has no Paperclip company — marketplace deploy blocked. Re-run org provisioning."
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        adapter_config = synthesize_openclaw_adapter(
            gateway_url=_ws_gateway_url(settings.ENVIRONMENT or ""),
            service_token=service_token.mint(owner_id),
            user_id=buyer_user_id,
        )
        fields = resolve_create_agent_fields(role=role, meta=meta)
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            adapter_type=OPENCLAW_GATEWAY_TYPE,
            adapter_config=adapter_config,
            session_cookie=admin_cookie,
            **fields,
        )
    finally:
        await http.aclose()


async def delete_paperclip_for_marketplace(
    *,
    owner_id: str,
    agent_id: str,
) -> None:
    """Cleanup partner to ``create_paperclip_for_marketplace`` for the
    case where Paperclip create succeeded but the catalog deploy failed
    afterwards. Idempotent — Paperclip's delete_agent swallows 404, so
    a missing or already-cleaned agent is a no-op.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.info(
            "delete_paperclip_for_marketplace: owner %s has no Paperclip company — skip",
            owner_id,
        )
        return
    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        await admin_client.delete_agent(
            session_cookie=admin_cookie,
            agent_id=agent_id,
        )
    finally:
        await http.aclose()
