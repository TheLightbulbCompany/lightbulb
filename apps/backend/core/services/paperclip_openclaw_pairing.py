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

import httpx

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


def _is_org_owner_id(owner_id: str) -> bool:
    """Clerk org IDs are prefixed 'org_'; user IDs are prefixed 'user_'.

    Stable convention from Clerk; if either prefix changes upstream,
    update this and the tests together.
    """
    return owner_id.startswith("org_")


async def resolve_company_id_for_owner(*, owner_id: str, repo: PaperclipRepo) -> str | None:
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
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        return []

    try:
        cookie = await get_admin_session_cookie(admin_client._http)
        agents = await admin_client.list_agents(
            session_cookie=cookie,
            company_id=company_id,
        )
    except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
        # Specific catch (not bare ``Exception``): the helper's contract
        # is "fall back to empty list on Paperclip availability issues"
        # so container provision never breaks because Paperclip is down,
        # the network is flaky, or admin credentials failed to load.
        # Three concrete failure modes covered:
        #   - ``PaperclipApiError``      — Paperclip returned 4xx/5xx.
        #   - ``httpx.HTTPError``        — transport-layer failures
        #     (ConnectError, ReadTimeout, etc. — all subclass HTTPError).
        #   - ``RuntimeError``           — credential-loading failures
        #     from ``get_admin_session_cookie`` (Secrets Manager miss,
        #     malformed payload, missing Set-Cookie).
        # Anything else (e.g. KeyError, programming bugs) is NOT caught
        # here so it surfaces in CloudWatch instead of being silently
        # masked as "Paperclip unavailable".
        logger.warning(
            "paperclip_openclaw_pairing: list_agents failed for owner=%s company=%s -- falling back to empty list: %s",
            owner_id,
            company_id,
            e,
        )
        return []

    if not isinstance(agents, list):
        logger.warning(
            "paperclip_openclaw_pairing: list_agents returned non-list for owner=%s -- got %s",
            owner_id,
            type(agents).__name__,
        )
        return []

    return agents


async def _resolve_seeded_agent_ids(*, owner_id: str, repo: PaperclipRepo) -> set[str]:
    """Resolve the persisted seeded-agent IDs (Friday + Atlas) for an owner.

    Source of truth is the persisted ``seeded_agent_ids`` captured on the
    provisioner's ``paperclip-companies`` row at ``provision_org`` time —
    NOT a role/name heuristic. A user-created agent that happens to use
    role ``ceo`` or the name ``Atlas`` is therefore never mistaken for a
    seeded one, and renaming a seeded agent never drops its protection.

    Owner keying mirrors ``resolve_company_id_for_owner``: personal owners
    are their own row key; for orgs the seeded IDs live on the
    provisioner's row, so we scan the org's rows for the one that carries
    them.

    RAISES on lookup failure — callers choose the failure posture
    (``get_seeded_agent_ids`` fails open for UI labeling;
    ``is_seeded_agent`` fails closed for delete enforcement).
    """
    if _is_org_owner_id(owner_id):
        for row in await repo.list_by_org_id(owner_id):
            ids = row.get("seeded_agent_ids")
            if ids:
                return set(ids)
        return set()
    row = await repo.get(owner_id)
    return set(row.seeded_agent_ids) if row and row.seeded_agent_ids else set()


async def get_seeded_agent_ids(*, owner_id: str, repo: PaperclipRepo) -> set[str]:
    """Best-effort seeded-id set for **UI labeling** (the Teams ``seeded``
    stamp). Returns an empty set on any lookup failure — a cosmetic stamp must
    never break an agent read. Do NOT use this for delete enforcement; use
    ``is_seeded_agent``, which fails CLOSED.
    """
    try:
        return await _resolve_seeded_agent_ids(owner_id=owner_id, repo=repo)
    except Exception:  # noqa: BLE001 — labeling is cosmetic; never break a read
        return set()


async def is_seeded_agent(*, owner_id: str, agent_id: str) -> bool:
    """Whether ``agent_id`` is an Isol8-seeded agent (Friday or Atlas).

    This backs the **delete guards** (OpenClaw gateway + Teams REST), so it
    FAILS CLOSED: if the persisted-id lookup errors (e.g. a DynamoDB outage),
    we return ``True`` to block deletion rather than risk removing a seeded
    agent during the error window. A blocked delete is recoverable on retry;
    a deleted Friday/Atlas breaks the provisioning contract.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    try:
        ids = await _resolve_seeded_agent_ids(owner_id=owner_id, repo=repo)
    except Exception:  # noqa: BLE001 — fail closed: block delete on lookup error
        logger.warning(
            "is_seeded_agent: seeded-id lookup failed for owner=%s agent=%s — failing closed (treating as seeded)",
            owner_id,
            agent_id,
            exc_info=True,
        )
        return True
    return agent_id in ids


async def register_paperclip_agent_devices(
    *,
    owner_id: str,
    gateway_token: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> list[dict[str, Any]]:
    """Build ``paired.json`` operator entries for the owner's Paperclip agents,
    and bind each agent's ``adapterConfig.authToken`` to the container gateway
    token. Returns the entries to fold into ``build_device_paired_json``.

    Called at container provision (the per-container ``gateway_token`` is only
    known then). For each ``openclaw_gateway`` agent carrying an Isol8-minted
    ``devicePrivateKeyPem``:

    1. Derive its device identity (public key + deviceId) from the PEM.
    2. Build a paired operator entry (operator.admin/read/write) bound to the
       container ``gateway_token`` — so the agent's own device clears the
       remote-operator pairing wall on its first relayed connect.
    3. Rewrite ``adapterConfig.authToken`` to ``gateway_token`` so the adapter's
       signed connect (the signature is computed *over* ``auth.token``)
       authenticates against the container's token-mode gateway. The URL keeps
       ``?token=<service JWT>`` for the API Gateway authorizer.

    Best-effort: Paperclip availability / per-agent failures are logged and
    skipped rather than failing container provision. Returns whatever entries
    were built.
    """
    from core.crypto.operator_device import (
        BACKEND_OPERATOR_SCOPES,
        build_paired_operator_entry,
        device_identity_from_pem,
    )
    from core.services.paperclip_adapter_config import OPENCLAW_GATEWAY_TYPE

    agents = await fetch_paperclip_agents_for_owner(
        owner_id=owner_id, repo=repo, admin_client=admin_client
    )
    if not agents:
        return []

    try:
        cookie = await get_admin_session_cookie(admin_client._http)
    except (RuntimeError, httpx.HTTPError) as e:
        logger.warning(
            "register_paperclip_agent_devices: admin cookie failed for owner=%s — skipping: %s",
            owner_id,
            e,
        )
        return []

    entries: list[dict[str, Any]] = []
    for agent in agents:
        if agent.get("adapterType") != OPENCLAW_GATEWAY_TYPE:
            continue
        cfg = agent.get("adapterConfig") or {}
        pem = cfg.get("devicePrivateKeyPem")
        if not pem:
            # Legacy / ephemeral-key agent — no stable Isol8-minted device to pair.
            continue
        try:
            identity = device_identity_from_pem(pem)
        except Exception as e:  # noqa: BLE001 — one bad key must not drop the rest
            logger.warning(
                "register_paperclip_agent_devices: bad device PEM for agent=%s: %s",
                agent.get("id"),
                e,
            )
            continue

        entries.append(
            build_paired_operator_entry(
                identity,
                gateway_token=gateway_token,
                scopes=BACKEND_OPERATOR_SCOPES,
                display_name=f"paperclip:{agent.get('name') or agent.get('id')}",
            )
        )

        # Bind the agent's connect token to the container gateway token.
        # Idempotent: skip the round-trip if already bound.
        if cfg.get("authToken") != gateway_token:
            try:
                await admin_client.patch_agent(
                    session_cookie=cookie,
                    agent_id=agent["id"],
                    body={
                        "adapterConfig": {**cfg, "authToken": gateway_token},
                        "replaceAdapterConfig": True,
                    },
                )
            except (PaperclipApiError, httpx.HTTPError) as e:
                logger.warning(
                    "register_paperclip_agent_devices: authToken bind failed for agent=%s: %s",
                    agent["id"],
                    e,
                )

    return entries


async def pair_with_openclaw(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_pool: Any,
    ip: str,
    token: str,
    extra_update_fields: dict[str, Any] | None = None,
) -> None:
    """Pair one Paperclip agent with the owner's OpenClaw container.

    Calls OpenClaw's ``agents.create`` RPC with ``name=<paperclip_uuid>``
    (so OpenClaw derives ``agentId`` from the UUID via
    ``normalizeAgentId``), then ``agents.update`` to set the human-
    friendly display name plus any caller-supplied extras.

    ``extra_update_fields`` — optional dict folded into the
    ``agents.update`` params. Used by marketplace deploys to thread the
    full slice-derived agent entry (identity, skills, reasoningDefault,
    workspace path, etc.) through the same RPC instead of file-merging
    it into ``openclaw.json`` and waiting for chokidar to reload. The
    helper always overrides ``agentId`` (to ``pc_uuid``) and ``name``
    (to ``friendly_name``), so callers can include them in the dict and
    they'll be ignored — the helper is the source of truth for both.

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
                "pair_with_openclaw: agent %s already exists in container for owner %s — skipping update",
                pc_uuid,
                owner_id,
            )
            return
        raise

    # Build update params: start from caller extras (if any), then
    # overlay the helper-owned fields (agentId, name) so they always
    # win. Caller cannot accidentally re-point the update at a
    # different agent or rename it back to the UUID.
    update_params: dict[str, Any] = dict(extra_update_fields) if extra_update_fields else {}
    update_params["agentId"] = pc_uuid
    update_params["name"] = friendly_name

    await gateway_pool.send_rpc(
        user_id=owner_id,  # gateway pool is keyed by owner_id
        req_id=f"pair-update-{secrets.token_hex(8)}",
        method="agents.update",
        params=update_params,
        ip=ip,
        token=token,
    )
