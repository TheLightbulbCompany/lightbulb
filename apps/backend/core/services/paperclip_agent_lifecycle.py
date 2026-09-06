"""Chat-initiated Paperclip agent lifecycle.

When /chat intercepts an ``agents.create`` or ``agents.delete`` RPC it
needs to mirror the operation against Paperclip — otherwise the next
container reprovision rebuilds ``openclaw.json`` from the live Paperclip
list and resurrects the supposedly-deleted agent (or fails to find a
freshly-created one).

Lives in core/services/ so the router stays on its side of the
routers→services boundary and reuses ``core/services/paperclip_admin_session``
for ``httpx.AsyncClient`` + admin-cookie management.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import service_token
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_agent_idempotency import (
    build_agent_create_idempotency_key,
    paperclip_agent_intent,
)
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_meta import PaperclipMeta, resolve_create_agent_fields
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
from core.services.paperclip_provisioning import _ws_gateway_url

logger = logging.getLogger(__name__)

_TERMINAL_ISSUE_STATUSES = {"done", "cancelled"}
_ISSUE_PAGE_SIZE = 1000
_AGENT_DELETE_LOCK_RETRY_DELAYS_S: tuple[float, ...] = (0.25, 1.0)
_OPENCLAW_FILE_LOCK_TIMEOUT_MARKER = "code=file_lock_timeout"


def _rows(payload: Any, key: str) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get(key) or payload.get("items") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


async def disarm_owner_nightly_automation(owner_id: str) -> dict[str, int]:
    """Pause a de-entitled owner's nightly CEO routine and cancel the
    in-progress issues that would retry-loop against their deleted container.

    Called from entitlement teardown after the container is gone. The Paperclip
    scheduler fires a routine while ``routines.status='active'`` (no
    company-disabled gate — fork ``tickScheduledTriggers``) and its heartbeat
    runner retry-loops ``in_progress`` issues, so a cancelled owner whose
    routine stays active reproduces the storm where every fire times out on the
    dead container. Pauses the routine (NOT archive — it may re-arm on
    re-subscribe) and cancels the looping issues.

    Idempotent: an already-paused/archived routine and terminal issues are
    skipped. Scoped to the nightly CEO routine by title; issue cancellation
    covers every in-progress issue for the company (nothing can progress once
    the container is gone), which also stops the productivity-review watchdog
    that amplifies the storm. May raise; its one caller (entitlement teardown)
    runs it as a guarded best-effort step.
    """
    from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        return {"routines_paused": 0, "issues_cancelled": 0}

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)

        routines = _rows(
            await admin.list_routines(company_id=company_id, session_cookie=cookie),
            "routines",
        )
        routines_paused = 0
        for routine in routines:
            if routine.get("title") != NIGHTLY_CEO_ROUTINE_TITLE or routine.get("status") != "active":
                continue
            await admin.patch_routine(
                routine_id=routine["id"],
                body={"status": "paused"},
                session_cookie=cookie,
            )
            routines_paused += 1

        # Snapshot every page before mutating: cancelling shrinks the
        # status=in_progress result set, so offset paging interleaved with the
        # cancels would skip rows (same reason prepare_paperclip_agent_delete
        # snapshots first).
        in_progress: list[dict] = []
        offset = 0
        while True:
            page = _rows(
                await admin.list_issues(
                    company_id=company_id,
                    session_cookie=cookie,
                    params={
                        "status": "in_progress",
                        "limit": str(_ISSUE_PAGE_SIZE),
                        "offset": str(offset),
                    },
                ),
                "issues",
            )
            in_progress.extend(page)
            if len(page) < _ISSUE_PAGE_SIZE:
                break
            offset += len(page)

        issues_cancelled = 0
        for issue in in_progress:
            if issue.get("status") != "in_progress":
                continue
            await admin.patch_issue(
                issue_id=issue["id"],
                body={"status": "cancelled"},
                session_cookie=cookie,
            )
            issues_cancelled += 1

        logger.info(
            "paperclip nightly disarm owner=%s company=%s routines_paused=%d issues_cancelled=%d",
            owner_id,
            company_id,
            routines_paused,
            issues_cancelled,
        )
        return {"routines_paused": routines_paused, "issues_cancelled": issues_cancelled}
    finally:
        await http.aclose()


def _is_openclaw_file_lock_timeout(exc: RuntimeError) -> bool:
    """Return whether OpenClaw rejected a config mutation on its file lock."""

    # Pinned OpenClaw defines FILE_LOCK_TIMEOUT_ERROR_CODE in
    # src/plugin-sdk/file-lock.ts; agents-config-mutations.ts sends deletes
    # through that lock. The gateway currently flattens the typed upstream
    # error into RuntimeError text, so retain its stable code marker here.
    return _OPENCLAW_FILE_LOCK_TIMEOUT_MARKER in str(exc).lower()


async def delete_runtime_agent_with_lock_retry(
    *,
    gateway: Any,
    owner_id: str,
    agent_id: str,
    ip: str,
    token: str,
) -> Any:
    """Delete from a running OpenClaw, retrying only its transient file lock."""

    total_attempts = len(_AGENT_DELETE_LOCK_RETRY_DELAYS_S) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return await gateway.send_rpc(
                user_id=owner_id,
                req_id=f"agent-delete-{agent_id}-{uuid.uuid4().hex[:8]}",
                method="agents.delete",
                params={"agentId": agent_id, "deleteFiles": True},
                ip=ip,
                token=token,
            )
        except RuntimeError as exc:
            if not _is_openclaw_file_lock_timeout(exc) or attempt == total_attempts:
                raise
            delay_s = _AGENT_DELETE_LOCK_RETRY_DELAYS_S[attempt - 1]
            logger.warning(
                "OpenClaw config lock blocked agent delete for owner=%s agent=%s (attempt %d/%d); retrying in %.2fs",
                owner_id,
                agent_id,
                attempt,
                total_attempts,
                delay_s,
            )
            await asyncio.sleep(delay_s)


async def prepare_paperclip_agent_delete(
    *,
    owner_id: str,
    agent_id: str,
) -> dict[str, int | str | None]:
    """Preserve live Paperclip work before an agent row is removed.

    Paperclip's native agent delete nulls issue assignees and hard-deletes the
    agent's routines.  Reassign non-terminal issues and move every routine to a
    surviving seed agent first; non-archived routines are archived so they can
    no longer fire under the replacement. The operation is idempotent.
    """

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        return {"replacement_agent_id": None, "issues_reassigned": 0, "routines_archived": 0}

    # Seed identity comes from the persisted provision record, never role/name.
    from core.services.paperclip_openclaw_pairing import get_seeded_agent_ids

    seeded_ids = await get_seeded_agent_ids(owner_id=owner_id, repo=repo)

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)

        agents = _rows(
            await admin.list_agents(company_id=company_id, session_cookie=cookie),
            "agents",
        )
        surviving_seeds = [a for a in agents if a.get("id") in seeded_ids and a.get("id") != agent_id]
        # Friday is the CEO seed. Role is only used to choose between already-
        # proven seed IDs, never to infer seed provenance.
        replacement = next((a for a in surviving_seeds if a.get("role") == "ceo"), None)
        replacement = replacement or next(iter(surviving_seeds), None)
        replacement_agent_id = replacement.get("id") if replacement else None

        routines = _rows(
            await admin.list_routines(company_id=company_id, session_cookie=cookie),
            "routines",
        )
        routines_archived = 0
        for routine in routines:
            if routine.get("assigneeAgentId") != agent_id:
                continue
            body: dict[str, Any] = {"assigneeAgentId": replacement_agent_id}
            if routine.get("status") != "archived":
                body["status"] = "archived"
            await admin.patch_routine(
                routine_id=routine["id"],
                body=body,
                session_cookie=cookie,
            )
            routines_archived += 1

        # Snapshot every page before mutation. Reassigning while paginating
        # would shrink the assignee-filtered result set and make an offset skip
        # the next page.
        issues: list[dict] = []
        offset = 0
        while True:
            page = _rows(
                await admin.list_issues(
                    company_id=company_id,
                    session_cookie=cookie,
                    params={
                        "assigneeAgentId": agent_id,
                        "limit": str(_ISSUE_PAGE_SIZE),
                        "offset": str(offset),
                    },
                ),
                "issues",
            )
            issues.extend(page)
            if len(page) < _ISSUE_PAGE_SIZE:
                break
            offset += len(page)

        issues_reassigned = 0
        for issue in issues:
            if issue.get("status") in _TERMINAL_ISSUE_STATUSES:
                continue
            await admin.patch_issue(
                issue_id=issue["id"],
                body={"assigneeAgentId": replacement_agent_id},
                session_cookie=cookie,
            )
            issues_reassigned += 1

        return {
            "replacement_agent_id": replacement_agent_id,
            "issues_reassigned": issues_reassigned,
            "routines_archived": routines_archived,
        }
    finally:
        await http.aclose()


async def delete_agent_everywhere(*, owner_id: str, agent_id: str) -> dict[str, Any]:
    """Delete a non-seed agent across Paperclip, OpenClaw, config, and EFS.

    The OpenClaw step happens before Paperclip removal. If it fails, the
    Paperclip row remains visible and a retry can reconcile the operation. If
    the container is not running, removing the persisted config entry is the
    authoritative OpenClaw deletion for its next boot.
    """

    from core.containers import get_ecs_manager, get_gateway_pool, get_workspace
    from core.containers.workspace import cleanup_deleted_agent
    from core.services.config_patcher import remove_from_openclaw_config_list
    from core.services.paperclip_openclaw_pairing import is_seeded_agent

    if await is_seeded_agent(owner_id=owner_id, agent_id=agent_id):
        raise PermissionError("Seeded agents (Friday/Atlas) cannot be deleted.")

    preserved = await prepare_paperclip_agent_delete(owner_id=owner_id, agent_id=agent_id)

    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)
    if container is not None and ip:
        gateway = get_gateway_pool()
        live_agents = _rows(
            await gateway.send_rpc(
                user_id=owner_id,
                req_id=f"agent-list-before-delete-{agent_id}-{uuid.uuid4().hex[:8]}",
                method="agents.list",
                params={},
                ip=ip,
                token=container["gateway_token"],
            ),
            "agents",
        )
        if any(agent.get("id") == agent_id for agent in live_agents):
            await delete_runtime_agent_with_lock_retry(
                gateway=gateway,
                owner_id=owner_id,
                agent_id=agent_id,
                ip=ip,
                token=container["gateway_token"],
            )

    # Belt-and-suspenders persistence cleanup: gateway deletion updates a
    # running process, while this guarantees a stopped/restarted container
    # cannot reload the removed entry from openclaw.json.
    await remove_from_openclaw_config_list(
        owner_id,
        ["agents", "list"],
        lambda entry: isinstance(entry, dict) and entry.get("id") == agent_id,
    )
    # cleanup_deleted_agent is intentionally best-effort, but the sidecar is
    # also the marketplace's install record. Remove it strictly before the
    # Paperclip handle disappears so a failed unlink leaves a visible, retryable
    # agent instead of a stale installed marker with no owner-facing handle.
    await asyncio.to_thread(get_workspace().remove_template_sidecar, owner_id, agent_id)
    await cleanup_deleted_agent(owner_id, agent_id)
    await delete_paperclip_for_chat(owner_id=owner_id, agent_id=agent_id)
    return {"deleted": True, "agent_id": agent_id, **preserved}


async def create_paperclip_for_chat(
    *,
    creator_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
    model: str | None,
    meta: PaperclipMeta | None = None,
    idempotency_replay_only: bool = False,
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
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
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
        fields = resolve_create_agent_fields(role=role, meta=meta)
        idempotency_key = build_agent_create_idempotency_key(
            operation="chat",
            owner_id=owner_id,
            intent=paperclip_agent_intent(
                name=name,
                adapter_type=OPENCLAW_GATEWAY_TYPE,
                **fields,
            ),
        )
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            adapter_type=OPENCLAW_GATEWAY_TYPE,
            adapter_config=adapter_config,
            session_cookie=admin_cookie,
            idempotency_key=idempotency_key,
            idempotency_replay_only=idempotency_replay_only,
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
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
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
    reports_to: str | None = None,
    idempotency_key: str,
    idempotency_replay_only: bool = False,
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
    ``reports_to``: optional Paperclip agent id the new hire reports to
        (its manager in the org chart, chosen by the buyer at Hire time).
        None leaves the new agent unparented, matching Paperclip's default.
    ``idempotency_key``: stable identity of the marketplace, approval, or
        builder operation. The caller owns that business identity.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
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
            reports_to=reports_to,
            idempotency_key=idempotency_key,
            idempotency_replay_only=idempotency_replay_only,
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
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
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
