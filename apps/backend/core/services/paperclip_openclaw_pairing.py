"""Paperclip <-> OpenClaw agent pairing helpers.

Single source of truth for the policy that:

1. Fetches the live Paperclip agent list for an OWNER (used at
   container provision time to populate openclaw.json's agents.list).

2. Pairs a single Paperclip agent with the OpenClaw container by
   calling agents.create + agents.update RPCs over the gateway.

The container is the runtime; Paperclip is the master record. Owner
keying matches the rest of the platform: org_id for orgs, user_id
for personal users. One container per owner, shared across all org
members.

Anything that creates a Paperclip agent should also call
``pair_with_openclaw`` when the container is reachable. Anything
that provisions a container should call
``fetch_paperclip_agents_for_owner`` first and pass the result into
``write_openclaw_config``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


def _is_org_owner_id(owner_id: str) -> bool:
    """Clerk org IDs are prefixed 'org_'; user IDs are prefixed 'user_'.

    Stable convention from Clerk; if either prefix changes upstream,
    update this and the tests together.
    """
    return owner_id.startswith("org_")


async def _resolve_company_id_for_owner(
    *, owner_id: str, repo: PaperclipRepo
) -> str | None:
    """Look up the Paperclip company_id for this owner.

    For personal users (owner_id = user_id): repo.get(user_id).company_id
    For orgs (owner_id = org_id): repo.get_org_company_id(org_id)

    Returns None if no company exists yet (provisioning hasn't run).
    """
    if _is_org_owner_id(owner_id):
        return await repo.get_org_company_id(owner_id)
    row = await repo.get(owner_id)
    return row.company_id if row else None


async def fetch_paperclip_agents_for_owner(
    *,
    owner_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> list[dict[str, Any]]:
    """Return the live Paperclip agent list for ``owner_id``, or [] if
    none can be fetched.

    Returns [] (not raises) when:
      - The owner has no Paperclip company yet (provisioning didn't run).
      - Paperclip's API is unreachable or returns an error.

    Container provision is the primary caller. We refuse to fail
    provision on Paperclip availability -- empty list flows through to
    the legacy 'main' agent fallback in build_openclaw_config_dict.
    """
    company_id = await _resolve_company_id_for_owner(
        owner_id=owner_id, repo=repo
    )
    if company_id is None:
        return []

    try:
        cookie = await get_admin_session_cookie(admin_client._http)
        agents = await admin_client.list_agents(
            session_cookie=cookie,
            company_id=company_id,
        )
    except Exception as e:  # noqa: BLE001 — best-effort fetch; see below.
        # Broad catch is intentional. The helper's contract is "fall back
        # to empty list on ANY Paperclip-side issue" so container provision
        # never breaks because Paperclip is down, the network is flaky,
        # or admin credentials failed to load. PaperclipApiError, transient
        # httpx errors (ConnectError/timeouts), and pre-request credential
        # exceptions all map to the same recovery path.
        logger.warning(
            "paperclip_openclaw_pairing: list_agents failed for owner=%s "
            "company=%s -- falling back to empty list: %s",
            owner_id,
            company_id,
            e,
        )
        return []

    if not isinstance(agents, list):
        logger.warning(
            "paperclip_openclaw_pairing: list_agents returned non-list for "
            "owner=%s -- got %s",
            owner_id,
            type(agents).__name__,
        )
        return []

    return agents


async def pair_with_openclaw(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_pool: Any,
    ip: str,
    token: str,
) -> None:
    """Pair one Paperclip agent with the owner's OpenClaw container.

    Calls OpenClaw's ``agents.create`` RPC with ``name=<paperclip_uuid>``
    (so OpenClaw derives ``agentId`` from the UUID via
    ``normalizeAgentId``), then ``agents.update`` to set the human-
    friendly display name.

    Idempotent: an "agent already exists" error from agents.create is
    swallowed (the agent was paired by a previous run or by the
    provision-time openclaw.json write).

    Other errors propagate so the caller (an HTTP handler or RPC
    interceptor) can return them to the user. Container reachability
    is the caller's responsibility — this helper assumes the gateway
    pool can reach the owner's container.

    Note: ``gateway_pool.send_rpc`` takes a ``user_id`` kwarg by name,
    but the gateway pool is keyed by container owner. Always pass the
    owner_id there.
    """
    pc_uuid = paperclip_agent["id"]
    friendly_name = paperclip_agent.get("name") or pc_uuid

    create_params = {
        "name": pc_uuid,
        "workspace": f"/home/node/.openclaw/workspaces/{pc_uuid}",
    }
    if paperclip_agent.get("model"):
        create_params["model"] = paperclip_agent["model"]

    try:
        await gateway_pool.send_rpc(
            user_id=owner_id,  # gateway pool is keyed by owner_id
            req_id=f"pair-create-{secrets.token_hex(8)}",
            method="agents.create",
            params=create_params,
            ip=ip,
            token=token,
        )
    except RuntimeError as e:
        # OpenClaw returns INVALID_REQUEST with "already exists" when the
        # agent is registered. Treat as idempotent success.
        if "already exists" in str(e):
            logger.info(
                "pair_with_openclaw: agent %s already exists in container "
                "for owner %s — skipping update",
                pc_uuid,
                owner_id,
            )
            return
        raise

    await gateway_pool.send_rpc(
        user_id=owner_id,  # gateway pool is keyed by owner_id
        req_id=f"pair-update-{secrets.token_hex(8)}",
        method="agents.update",
        params={
            "agentId": pc_uuid,
            "name": friendly_name,
        },
        ip=ip,
        token=token,
    )
