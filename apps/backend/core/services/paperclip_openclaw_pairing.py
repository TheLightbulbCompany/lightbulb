"""Paperclip roster helpers — the owner-keyed view of the master agent record.

Single source of truth for reading an OWNER's live Paperclip roster: the agent
list, the seeded/starter id sets, and the company-id resolution every
Paperclip-facing service needs. Owner keying matches the rest of the platform:
``org_id`` for orgs, ``user_id`` for personal users.

The OpenClaw half this module was named for (``pair_with_openclaw``,
``ensure_operator_device_paired``, ``register_paperclip_agent_devices``, the
trust-store approval channel, and the operator-device keypair they all minted) is
gone: a Hermes agent's "pairing" is its S3 profile
(``hermes_provisioning.write_profile_for_agent`` — config, Paperclip key, SOUL), and the native
adapter (``paperclip_adapter_config.build_hermes_gateway_adapter_config``) needs no device
identity. The filename is a scar; renaming it would churn 25 import sites for no behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


async def resolve_company_id_for_owner(*, owner_id: str, repo: PaperclipRepo) -> str | None:
    """The Paperclip company_id of ``owner_id``'s ACTIVE workspace row, else None.

    None when no company exists yet (provisioning hasn't run) OR the workspace
    is not ``active`` — a reconcile can persist a ``failed`` row carrying a
    real adopted ``company_id``, and every caller (paperclip_agent_lifecycle,
    marketplace_listings, company_deploy_service, agent_export, ...) guards
    with ``if company_id is None`` into its clean no-company branch instead of
    a doomed Paperclip call.
    """
    ws = await repo.resolve_owner_row(owner_id)
    return (ws.company_id or None) if ws is not None else None


async def fetch_paperclip_agents_for_owner(
    *,
    owner_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
    raise_on_unavailable: bool = False,
) -> list[dict[str, Any]]:
    """Return the live Paperclip agent list for ``owner_id``, or [] if
    none can be fetched.

    Returns [] (not raises) when:
      - The owner has no Paperclip company yet (provisioning didn't run).
      - Paperclip's API is unreachable or returns an error (default mode).

    Container provision is the primary caller. We refuse to fail
    provision on Paperclip availability -- empty list flows through to
    the legacy 'main' agent fallback in build_openclaw_config_dict.

    ``raise_on_unavailable=True`` re-raises availability failures instead of
    swallowing them into []: the Friday-publish guard treats an empty list as
    "allow the export", so for it an outage masked as [] fails OPEN — a
    seller could snapshot Friday (owner-keyed identity + memory) into an
    immutable artifact while Paperclip is down. No-company-yet still returns
    [] in both modes — that's a genuine empty roster, not an outage.
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
        if raise_on_unavailable:
            raise
        logger.warning(
            "paperclip_openclaw_pairing: list_agents failed for owner=%s company=%s -- falling back to empty list: %s",
            owner_id,
            company_id,
            e,
        )
        return []

    if not isinstance(agents, list):
        # A 200 with a non-list payload is as unknowable as an outage: the
        # roster cannot be trusted to be empty. Strict mode must not let it
        # degrade to [] — the Friday guard reads [] as "allow the export".
        if raise_on_unavailable:
            raise PaperclipApiError(
                "list_agents returned a non-list payload",
                status_code=502,
                body=type(agents).__name__,
            )
        logger.warning(
            "paperclip_openclaw_pairing: list_agents returned non-list for owner=%s -- got %s",
            owner_id,
            type(agents).__name__,
        )
        return []

    return agents


async def _resolve_seeded_agent_ids(*, owner_id: str, repo: PaperclipRepo) -> set[str]:
    """Resolve persisted legacy founder-seed IDs (Friday + Atlas) for an owner.

    Source of truth is the persisted ``seeded_agent_ids`` on the workspace
    row — NOT a role/name heuristic. A user-created agent that happens to use
    role ``ceo`` or the name ``Atlas`` is therefore never mistaken for a
    seeded one, and renaming a seeded agent never drops its protection.

    RAISES on lookup failure — callers choose the failure posture
    (``get_seeded_agent_ids`` fails open for UI labeling;
    ``is_seeded_agent`` fails closed for delete enforcement).
    """
    ws = await repo.get_workspace(owner_id)
    return set(ws.seeded_agent_ids) if ws is not None and ws.seeded_agent_ids else set()


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


async def get_starter_agent_ids(*, owner_id: str, repo: PaperclipRepo) -> set[str]:
    """Best-effort starter UUIDs from the authoritative owner row."""
    try:
        row = await repo.resolve_owner_row(owner_id)
        return set(row.starter_agent_ids.values()) if row and row.starter_agent_ids else set()
    except Exception:  # noqa: BLE001 — provenance labeling must not break reads
        return set()


async def remove_starter_agent_id(*, owner_id: str, agent_id: str) -> None:
    """Forget a starter UUID after its correlated deletion succeeds."""
    await PaperclipRepo(table_name="paperclip-companies").remove_starter_agent_id(owner_id, agent_id)


async def is_seeded_agent(*, owner_id: str, agent_id: str) -> bool:
    """Whether ``agent_id`` is a legacy Isol8-seeded agent (Friday or Atlas).

    This backs the **delete guards** (OpenClaw gateway + Teams REST), so it
    FAILS CLOSED: if the persisted-id lookup errors (e.g. a DynamoDB outage),
    we return ``True`` to block deletion rather than risk removing a seeded
    agent during the error window. A blocked delete is recoverable on retry;
    deleting one would break its legacy provisioning contract.
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
