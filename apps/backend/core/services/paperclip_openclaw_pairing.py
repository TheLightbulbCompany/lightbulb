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

import asyncio
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

# A single dropped gateway RPC response (transient WebSocket teardown mid-call)
# surfaces as ``TimeoutError`` and would otherwise fail the whole pairing — and
# with it a marketplace Hire. One extra attempt heals it: ``agents.create`` is
# idempotent (an existing agent falls through to the rename below), so a retry
# after a half-completed first attempt completes the friendly-name update
# instead of leaving the agent stuck at ``name=<uuid>``.
_PAIR_MAX_ATTEMPTS = 2
_PAIR_RETRY_BACKOFF_S = 0.5

# Short bound for the display-name update when the caller has told us it is
# cosmetic (``display_update_is_cosmetic``). Measured on prod: a marketplace Hire
# took 85s because the container was mid-config-reload — agents.create answered in
# 10.9s, agents.update in 24.7s, and one response outran the RPC window entirely,
# so the pair retried and wrote config AGAIN. Each config write triggers another
# reload, and each reload re-adopts OAuth credentials per agent over the network
# (``fetch timeout after 5000ms … chatgpt.com`` in the container log), which is
# what blocks the event loop. 10s is comfortably above a healthy container's
# sub-second answer and far below the stalled case we refuse to wait for.
_COSMETIC_UPDATE_TIMEOUT_S = 10


def _is_org_owner_id(owner_id: str) -> bool:
    """Delegates to core.auth.is_org_owner_id — single edit point for the prefix convention."""
    from core.auth import is_org_owner_id

    return is_org_owner_id(owner_id)


async def resolve_company_id_for_owner(*, owner_id: str, repo: PaperclipRepo) -> str | None:
    """Look up the Paperclip company_id for this owner.

    For personal users (owner_id = user_id): the row's company_id, unless the
    row is a ``failed`` marker.
    For orgs (owner_id = org_id): the company_id of an ``active`` row, located
    by scanning the org's rows (``list_by_org_id``).

    Returns None when no company exists yet (provisioning hasn't run) OR the
    only row(s) are ``failed``. Every caller here guards with
    ``if company_id is None`` (paperclip_agent_lifecycle, marketplace_listings,
    company_deploy_service, agent_export), so returning None routes them into
    their clean no-company branch instead of a doomed Paperclip call. Two
    distinct failed shapes must both resolve to None:

      * personal / net-new: a ``failed`` marker carries ``company_id=""`` (the
        company was never created); the ``status == "failed"`` check below
        catches it (as does the empty-id check).
      * org reconcile: a ``failed`` row can now carry a REAL adopted
        ``company_id`` (the Friday-partial case), and ``get_org_company_id``
        returns the first row's id regardless of status — so org resolution
        deliberately does NOT use ``get_org_company_id`` here. It requires an
        ACTIVE row, so a failed org row (even one with a real company_id)
        resolves to None, mirroring the personal ``status == "failed"`` filter.
    """
    if _is_org_owner_id(owner_id):
        # Require an ACTIVE row: a reconcile can persist a ``failed`` org row
        # carrying a real adopted company_id, which get_org_company_id would
        # hand back regardless of status. Scan the org's rows (all active rows
        # share one company_id by the provisioner's invariant) and return the
        # first active, non-empty company_id; if none is active, return None.
        for row in await repo.list_by_org_id(owner_id):
            if row.get("status") == "active" and row.get("company_id"):
                return row["company_id"]
        return None
    row = await repo.get(owner_id)
    if row is None or row.status == "failed" or not row.company_id:
        return None
    return row.company_id


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
    from core.services.paperclip_adapter_config import (
        OPENCLAW_GATEWAY_TYPE,
        desired_agent_adapter_fields,
    )

    agents = await fetch_paperclip_agents_for_owner(owner_id=owner_id, repo=repo, admin_client=admin_client)
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

        # Bring the agent's per-agent adapterConfig fields current — see
        # `desired_agent_adapter_fields` for what each one does and why.
        # Idempotent: skip the round-trip when all of them already match.
        desired = desired_agent_adapter_fields(agent_id=agent["id"], gateway_token=gateway_token)
        if any(cfg.get(k) != v for k, v in desired.items()):
            try:
                await admin_client.patch_agent(
                    session_cookie=cookie,
                    agent_id=agent["id"],
                    body={
                        "adapterConfig": {**cfg, **desired},
                        "replaceAdapterConfig": True,
                    },
                )
            except (PaperclipApiError, httpx.HTTPError) as e:
                logger.warning(
                    "register_paperclip_agent_devices: adapterConfig bind failed for agent=%s: %s",
                    agent["id"],
                    e,
                )

    return entries


async def ensure_operator_device_paired(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_token: str,
) -> None:
    """Pair ONE post-provision agent's operator device into the RUNNING
    container's trust store, and bind its connect token to the gateway token.

    ``register_paperclip_agent_devices`` only enumerates the agents that exist
    AT container provision (it feeds the full ``paired.json`` write). An agent
    created against an already-running container is registered in OpenClaw's
    agent list by ``pair_with_openclaw`` but is NOT in ``devices/paired.json``
    and still carries ``authToken`` = the service JWT. Its first relayed connect
    is therefore rejected NOT_PAIRED (no device entry) with a token mismatch
    (the device signature is computed *over* ``auth.token``, which the token-mode
    gateway requires to equal the container's gateway token). This closes that
    gap live + idempotently so a freshly-created agent can connect immediately,
    without waiting for the next reprovision to enumerate it.

    Best-effort: every failure is logged, not raised — device pairing must never
    break the user-visible agent create. Callers invoke this right after
    ``pair_with_openclaw`` when the container is reachable; ``gateway_token`` is
    the same per-container token passed there.
    """
    from core.config import settings
    from core.containers import get_workspace
    from core.crypto.operator_device import (
        BACKEND_OPERATOR_SCOPES,
        build_paired_operator_entry,
        device_identity_from_pem,
    )
    from core.repositories import container_repo as _container_repo
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.desktop_pair import EfsWorkspaceAdapter
    from core.services.friday_identity import stage_paperclip_identity_for_agent
    from core.services.paperclip_adapter_config import (
        OPENCLAW_GATEWAY_TYPE,
        desired_agent_adapter_fields,
    )
    from core.services.paperclip_api_key import ensure_paperclip_api_key

    if paperclip_agent.get("adapterType") != OPENCLAW_GATEWAY_TYPE:
        return
    cfg = paperclip_agent.get("adapterConfig") or {}
    pem = cfg.get("devicePrivateKeyPem")
    if not pem:
        # Legacy / ephemeral-key agent — no stable Isol8-minted device to pair.
        return
    agent_id = paperclip_agent.get("id")

    try:
        identity = device_identity_from_pem(pem)
    except Exception as e:  # noqa: BLE001 — a bad PEM must not break the create
        logger.warning(
            "ensure_operator_device_paired: bad device PEM for agent=%s: %s",
            agent_id,
            e,
        )
        return

    entry = build_paired_operator_entry(
        identity,
        gateway_token=gateway_token,
        scopes=BACKEND_OPERATOR_SCOPES,
        display_name=f"paperclip:{paperclip_agent.get('name') or agent_id}",
    )

    # 1. Live, locked, idempotent merge into the container's paired.json. The
    #    same primitive the desktop-pair flow uses, so the in-container node's
    #    self-registration and admin writers serialize on one lock.
    def _mutate(paired: dict) -> bool:
        existing = paired.get(entry["deviceId"])
        if existing and existing.get("publicKey") == entry["publicKey"]:
            return False  # already paired with this key — preserve metadata
        paired[entry["deviceId"]] = entry
        return True

    try:
        adapter = EfsWorkspaceAdapter(
            owner_id=owner_id,
            workspace=get_workspace(),
            container_repo=_container_repo,
            gateway_ws_url=settings.WS_GATEWAY_URL,
        )
        await adapter.mutate_paired_json(_mutate)
    except Exception as e:  # noqa: BLE001 — best-effort; still try the token bind
        logger.warning(
            "ensure_operator_device_paired: paired.json merge failed for agent=%s: %s",
            agent_id,
            e,
        )

    # 2. Bring the agent's per-agent adapterConfig fields current — see
    #    `desired_agent_adapter_fields` for what each one does and why.
    #    Idempotent: skip the round-trip when all of them already match.
    desired = desired_agent_adapter_fields(agent_id=agent_id, gateway_token=gateway_token)
    if any(cfg.get(k) != v for k, v in desired.items()):
        try:
            async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
                admin = PaperclipAdminClient(http_client=http)
                cookie = await get_admin_session_cookie(http)
                await admin.patch_agent(
                    session_cookie=cookie,
                    agent_id=agent_id,
                    body={
                        "adapterConfig": {**cfg, **desired},
                        "replaceAdapterConfig": True,
                    },
                )
        except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
            logger.warning(
                "ensure_operator_device_paired: adapterConfig bind failed for agent=%s: %s",
                agent_id,
                e,
            )

    # 3. Mint + stage this agent's per-agent Paperclip identity file
    #    (paperclip-claimed-api-key.json) into its own workspace. The only other
    #    place this happens is write_user_configs at provision, which live-create
    #    flows (/chat, /teams, marketplace hire) never run — so without this the
    #    new agent's claimedApiKeyPath above points at a missing file and its
    #    first Paperclip wake can't authenticate until a full reprovision. Mirror
    #    the same Secrets-Manager key minting + company resolution, then write the
    #    file write-if-missing (idempotent). Best-effort: any failure here must
    #    not break the user-visible agent create — the next reprovision re-stages.
    try:
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
            repo = PaperclipRepo(table_name="paperclip-companies")
            admin = PaperclipAdminClient(http_client=http)
            token = await ensure_paperclip_api_key(
                owner_id=owner_id,
                paperclip_agent_id=agent_id,
                admin_client=admin,
            )
            company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
            await stage_paperclip_identity_for_agent(
                owner_id=owner_id,
                agent_id=agent_id,
                token=token or "",
                company_id=company_id,
                api_url=settings.PAPERCLIP_INTERNAL_URL or None,
                workspace=get_workspace(),
            )
    except Exception as e:  # noqa: BLE001 — staging must not break the agent create
        logger.warning(
            "ensure_operator_device_paired: paperclip identity staging failed for agent=%s: %s",
            agent_id,
            e,
        )


async def pair_with_openclaw(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_pool: Any,
    ip: str,
    token: str,
    extra_update_fields: dict[str, Any] | None = None,
    display_update_is_cosmetic: bool = False,
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

    ``display_update_is_cosmetic`` — set by callers that persist the same
    display fields (``name``, ``identity``) to ``openclaw.json`` themselves right
    after this returns, so the ``agents.update`` RPC only buys instant
    appearance, not correctness. For those callers the update gets a short
    timeout and a timeout is swallowed rather than retried. Default False keeps
    the strict behaviour for the chat / teams create paths, where this RPC pair
    IS the only write and a lost update strands the agent at ``name=<uuid>``.

    Note: ``gateway_pool.send_rpc`` takes a ``user_id`` kwarg by name,
    but the gateway pool is keyed by container owner. Always pass the
    owner_id there.
    """
    pc_uuid = paperclip_agent["id"]
    friendly_name = paperclip_agent.get("name") or pc_uuid

    create_params = {
        "name": pc_uuid,
        "workspace": f"/home/node/.openclaw/workspace-{pc_uuid}",
    }
    if paperclip_agent.get("model"):
        create_params["model"] = paperclip_agent["model"]

    # Build update params: start from caller extras (if any), then
    # overlay the helper-owned fields (agentId, name) so they always
    # win. Caller cannot accidentally re-point the update at a
    # different agent or rename it back to the UUID.
    update_params: dict[str, Any] = dict(extra_update_fields) if extra_update_fields else {}
    update_params["agentId"] = pc_uuid
    update_params["name"] = friendly_name

    # Bounded retry on a transient dropped RPC response only. ``CancelledError``
    # (cooperative task cancellation) is BaseException, not Exception, so it
    # propagates untouched — we never fight a real cancel. Any other RuntimeError
    # (a genuine schema/protocol rejection) also propagates immediately.
    for attempt in range(1, _PAIR_MAX_ATTEMPTS + 1):
        try:
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
                # agent is registered. Fall through to the rename rather than
                # returning early — a prior attempt that created the agent but
                # lost its update response left ``name=<uuid>``; applying the
                # update now heals it. agents.update is itself idempotent.
                if "already exists" not in str(e):
                    raise
                logger.info(
                    "pair_with_openclaw: agent %s already exists in container for owner %s — applying update to heal display name",
                    pc_uuid,
                    owner_id,
                )

            try:
                await gateway_pool.send_rpc(
                    user_id=owner_id,  # gateway pool is keyed by owner_id
                    req_id=f"pair-update-{secrets.token_hex(8)}",
                    method="agents.update",
                    params=update_params,
                    ip=ip,
                    token=token,
                    **({"timeout": _COSMETIC_UPDATE_TIMEOUT_S} if display_update_is_cosmetic else {}),
                )
            except TimeoutError:
                if not display_update_is_cosmetic:
                    raise
                # The caller writes these same display fields durably right
                # after us, so losing this RPC costs a few seconds of
                # ``name=<uuid>`` in the sidebar, not correctness. Swallowing it
                # is the whole point: re-raising would retry, and each retry
                # writes config again, which triggers another container reload —
                # the stall that made this RPC slow in the first place.
                logger.info(
                    "pair_with_openclaw: cosmetic display update timed out for owner=%s agent=%s "
                    "after %ss — leaving it to the caller's durable config write",
                    owner_id,
                    pc_uuid,
                    _COSMETIC_UPDATE_TIMEOUT_S,
                )
            return
        except TimeoutError:
            if attempt >= _PAIR_MAX_ATTEMPTS:
                raise
            logger.warning(
                "pair_with_openclaw: agents RPC timed out for owner=%s agent=%s (attempt %d/%d) — retrying",
                owner_id,
                pc_uuid,
                attempt,
                _PAIR_MAX_ATTEMPTS,
            )
            await asyncio.sleep(_PAIR_RETRY_BACKOFF_S)
