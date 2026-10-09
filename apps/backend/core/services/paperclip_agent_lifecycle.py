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

import logging
from typing import Any

import httpx

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_agent_idempotency import (
    build_agent_create_idempotency_key,
    paperclip_agent_intent,
)
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_adapter_config import (
    HERMES_GATEWAY_TYPE,
    build_hermes_gateway_adapter_config,
)
from core.services.paperclip_meta import PaperclipMeta, resolve_create_agent_fields
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

_TERMINAL_ISSUE_STATUSES = {"done", "cancelled"}


async def resolve_agent_adapter(owner_id: str) -> tuple[str, dict]:
    """Which Paperclip adapter type + config carries an agent this door creates.

    Shared by every Paperclip agent-creation call site in the backend (chat-create and
    marketplace-deploy below; also ``core/services/company_deploy_service.py`` and
    ``routers/teams/agents.py``'s ``/agents`` endpoint) so the wiring lives in exactly one
    place. There is one answer: Paperclip's native ``hermes_gateway`` adapter through the
    authenticated backend facade, which needs no operator device pairing.

    It takes ONLY the owner id. The ``agent_label``/``gateway_user_id``/``runtime_host``
    arguments four call sites used to thread here went with the device-paired adapter: the
    native one derives the real agent id from Paperclip's signed run context and sends it in
    ``X-Hermes-Session-Key``, and there is one host.
    """
    container = await container_repo.get_by_owner_id(owner_id)
    return HERMES_GATEWAY_TYPE, build_hermes_gateway_adapter_config(
        owner_id, hook_token=(container or {}).get("hook_token") or ""
    )


async def list_hermes_gateway_agents(owner_id: str) -> tuple[str | None, list[str]]:
    """The owner's active Paperclip company id and its ``hermes_gateway`` agent ids -- what
    ``sync_agent_adapter_config`` would patch. ``(None, [])`` when no company exists yet."""
    company_id = await resolve_company_id_for_owner(
        owner_id=owner_id, repo=PaperclipRepo(table_name="paperclip-companies")
    )
    if company_id is None:
        return None, []
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        agents = _rows(await admin.list_agents(session_cookie=cookie, company_id=company_id), "agents")
    return company_id, [a["id"] for a in agents if a.get("adapterType") == HERMES_GATEWAY_TYPE and a.get("id")]


async def sync_agent_adapter_config(owner_id: str) -> int:
    """Rewrite every ``hermes_gateway`` agent's FULL adapter config from today's builder.

    ``build_hermes_gateway_adapter_config`` output (url, timeoutSec, pollIntervalMs,
    paperclipApiUrl, apiKey = the row's CURRENT ``hook_token``) replaces the stored config
    wholesale (``replaceAdapterConfig``), so any builder change -- e.g. #3065's timeoutSec
    900 -> 3600 -- reaches existing agents on the next sync. ``reconcile_runtime`` runs it on
    every reconcile; ``prepare_hermes_workspace`` also runs it when it mints a new hook token.

    The adapter config is a snapshot (Paperclip stores the apiKey as a managed company secret),
    and ``provision_org`` short-circuits on an already-active company -- so when an owner's
    containers row is re-created and ``prepare_hermes_workspace`` mints a new hook_token, the
    surviving company's agents keep the OLD one and every run 401s at the facade while room
    chat keeps working. This is the repair path for that.

    The token is read CONSISTENTLY here, right before the PATCHes, so two concurrent provisions
    converge on the row's latest token (last writer wins with the current value, never a stale
    one a caller captured). Idempotent. Returns the number patched (0 without a company).
    **Raises** on failure: a half-synced workspace looks healthy.
    """
    company_id, agent_ids = await list_hermes_gateway_agents(owner_id)
    if not agent_ids:
        return 0
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    adapter_config = build_hermes_gateway_adapter_config(owner_id, hook_token=row.get("hook_token") or "")
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        for agent_id in agent_ids:
            await admin.patch_agent(
                session_cookie=cookie,
                agent_id=agent_id,
                body={"adapterConfig": adapter_config, "replaceAdapterConfig": True},
            )
    logger.info("sync_agent_adapter_config: owner=%s company=%s patched=%d", owner_id, company_id, len(agent_ids))
    return len(agent_ids)


_ISSUE_PAGE_SIZE = 1000
FINAL_ROOT_CEO_CONFLICT = "The final root CEO cannot be deleted or demoted."


class AgentLifecycleConflict(RuntimeError):
    """A requested mutation would leave the company without a root CEO."""


def _rows(payload: Any, key: str) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get(key) or payload.get("items") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _is_root_ceo(agent: dict) -> bool:
    return agent.get("role") == "ceo" and not agent.get("reportsTo")


def ensure_root_ceo_survives(*, target: dict, agents: list[dict]) -> None:
    if _is_root_ceo(target) and not any(
        agent.get("id") != target.get("id") and _is_root_ceo(agent) for agent in agents
    ):
        raise AgentLifecycleConflict(FINAL_ROOT_CEO_CONFLICT)


def _heartbeat_enabled(agent: dict) -> bool:
    return ((agent.get("runtimeConfig") or {}).get("heartbeat") or {}).get("enabled") is True


async def list_all_issues(admin: PaperclipAdminClient, *, cookie: str, company_id: str, params: dict) -> list[dict]:
    """Every page of a filtered issue list. Snapshot ALL pages before mutating: cancelling
    shrinks a status-filtered result set, so offset paging interleaved with the cancels
    would skip rows (same reason ``prepare_paperclip_agent_delete`` snapshots first)."""
    issues: list[dict] = []
    offset = 0
    while True:
        page = _rows(
            await admin.list_issues(
                company_id=company_id,
                session_cookie=cookie,
                params={**params, "limit": str(_ISSUE_PAGE_SIZE), "offset": str(offset)},
            ),
            "issues",
        )
        issues.extend(page)
        if len(page) < _ISSUE_PAGE_SIZE:
            return issues
        offset += len(page)


async def pause_owner_automation(owner_id: str, *, dry_run: bool = False) -> dict[str, list[str]]:
    """THE seam that stops an owner's Paperclip company waking on its own once the owner
    is not live (cancelled, expired trial, runtime torn down).

    Paperclip's scheduler fires a routine while ``routine.status == "active"`` (fork
    ``routines.ts`` ``tickScheduledTriggers``) and an agent's timer heartbeat while
    ``runtimeConfig.heartbeat.enabled`` holds -- neither checks whether a runtime exists, so
    a dead workspace kept filing a BLOCKED task every night. This pauses every active routine
    (``PATCH /api/routines/{id} {status: "paused"}``) and turns off every agent's enabled timer
    heartbeat (all agents, not just the starters: a legacy roster's specialists fired too),
    recording exactly what it paused on the workspace row first.

    Callers: ``hermes_provisioning.delete_workspace_runtime`` (every runtime teardown),
    ``user_provision.arm_nightly_ceo``'s not-entitled branch (every billing reconcile), the
    expired-trial sweep (``durable_activity``), and ``disarm_owner_nightly_automation``.
    ``resume_owner_automation`` undoes it from ``arm_nightly_ceo``'s entitled branch.

    Idempotent (only active things are touched). Raises on failure; callers on a
    cancellation path guard it. ``dry_run`` reports without writing.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    ws = await repo.resolve_owner_row(owner_id, include_inactive=True)
    if ws is None or not ws.company_id:
        return {"agent_ids": [], "routine_ids": []}
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        agents = _rows(await admin.list_agents(session_cookie=cookie, company_id=ws.company_id), "agents")
        routines = _rows(await admin.list_routines(session_cookie=cookie, company_id=ws.company_id), "routines")
        agent_ids = [a["id"] for a in agents if a.get("id") and _heartbeat_enabled(a)]
        routine_ids = [r["id"] for r in routines if r.get("id") and r.get("status") == "active"]
        if not dry_run:
            await repo.record_paused_automation(owner_id, agent_ids=agent_ids, routine_ids=routine_ids)
            for agent_id in agent_ids:
                await admin.set_agent_heartbeat(session_cookie=cookie, agent_id=agent_id, heartbeat={"enabled": False})
            for routine_id in routine_ids:
                await admin.patch_routine(session_cookie=cookie, routine_id=routine_id, body={"status": "paused"})
    if not dry_run:
        put_metric("paperclip.automation.pause", dimensions={"outcome": "ran"})
        logger.info(
            "paperclip automation paused owner=%s heartbeats=%d routines=%d", owner_id, len(agent_ids), len(routine_ids)
        )
    return {"agent_ids": agent_ids, "routine_ids": routine_ids}


async def resume_owner_automation(owner_id: str) -> dict[str, int]:
    """Undo ``pause_owner_automation``: re-enable exactly the heartbeats and routines IT
    paused, then clear the record. A routine is re-activated only while it is still
    ``paused`` (the owner may have archived it since); an agent or routine that no longer
    exists is skipped. ``arm_nightly_ceo`` re-applies the starters' own heartbeat configs
    (interval, CEO opt-out) right after. Idempotent; raises on a Paperclip failure, leaving
    the record for the next reconcile."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    ws = await repo.resolve_owner_row(owner_id, consistent_read=True, include_inactive=True)
    if ws is None or not (
        ws.automation_paused_at or ws.automation_paused_agent_ids or ws.automation_paused_routine_ids
    ):
        return {"heartbeats_resumed": 0, "routines_resumed": 0}
    agent_ids = sorted(ws.automation_paused_agent_ids or ())
    routine_ids = sorted(ws.automation_paused_routine_ids or ())
    heartbeats = routines = 0
    if agent_ids or routine_ids:
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
            admin = PaperclipAdminClient(http_client=http)
            cookie = await get_admin_session_cookie(http)
            for agent_id in agent_ids:
                try:
                    await admin.set_agent_heartbeat(
                        session_cookie=cookie, agent_id=agent_id, heartbeat={"enabled": True}
                    )
                    heartbeats += 1
                except PaperclipApiError as e:
                    if e.status_code != 404:
                        raise
            for routine_id in routine_ids:
                try:
                    routine = await admin.get_routine(session_cookie=cookie, routine_id=routine_id)
                except PaperclipApiError as e:
                    if e.status_code != 404:
                        raise
                    continue
                if routine.get("status") == "paused":
                    await admin.patch_routine(session_cookie=cookie, routine_id=routine_id, body={"status": "active"})
                    routines += 1
    await repo.clear_paused_automation(owner_id, agent_ids=agent_ids, routine_ids=routine_ids)
    put_metric("paperclip.automation.resume", dimensions={"outcome": "ran"})
    logger.info("paperclip automation resumed owner=%s heartbeats=%d routines=%d", owner_id, heartbeats, routines)
    return {"heartbeats_resumed": heartbeats, "routines_resumed": routines}


async def disarm_owner_nightly_automation(owner_id: str) -> dict[str, int]:
    """Entitlement teardown's Paperclip step: ``pause_owner_automation``, then cancel the
    ``in_progress`` issues Paperclip's heartbeat runner would retry-loop against the deleted
    runtime. RE-SUBSCRIBE self-heals through ``resume_owner_automation`` (via
    ``arm_nightly_ceo``); cancelled issues stay cancelled. May raise; its one caller runs it
    as a guarded best-effort step."""
    paused = await pause_owner_automation(owner_id)
    repo = PaperclipRepo(table_name="paperclip-companies")
    ws = await repo.resolve_owner_row(owner_id, include_inactive=True)
    if ws is None or not ws.company_id:
        return {"heartbeats_paused": 0, "routines_paused": 0, "issues_cancelled": 0}
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        in_progress = await list_all_issues(
            admin, cookie=cookie, company_id=ws.company_id, params={"status": "in_progress"}
        )
        issues_cancelled = 0
        for issue in in_progress:
            if issue.get("status") != "in_progress":
                continue
            await admin.patch_issue(issue_id=issue["id"], body={"status": "cancelled"}, session_cookie=cookie)
            issues_cancelled += 1
    return {
        "heartbeats_paused": len(paused["agent_ids"]),
        "routines_paused": len(paused["routine_ids"]),
        "issues_cancelled": issues_cancelled,
    }


async def prepare_paperclip_agent_delete(
    *,
    owner_id: str,
    agent_id: str,
) -> dict[str, int | str | None]:
    """Preserve live Paperclip work before an agent row is removed.

    Paperclip's native agent delete nulls issue assignees and hard-deletes the
    agent's routines.  Reassign non-terminal issues and move every routine to a
    surviving agent first; non-archived routines are archived so they can
    no longer fire under the replacement. The operation is idempotent.
    """

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        return {"replacement_agent_id": None, "issues_reassigned": 0, "routines_archived": 0}

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)

        agents = _rows(
            await admin.list_agents(company_id=company_id, session_cookie=cookie),
            "agents",
        )
        target = next((agent for agent in agents if agent.get("id") == agent_id), None)
        survivors = [agent for agent in agents if agent.get("id") != agent_id]
        if target is not None:
            ensure_root_ceo_survives(target=target, agents=agents)
        replacement = next((agent for agent in survivors if _is_root_ceo(agent)), None)
        replacement = replacement or next(iter(survivors), None)
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


async def _refresh_facts(owner_id: str) -> None:
    from core.services.owner_facts import write_owner_facts

    await write_owner_facts(owner_id)  # best-effort, never raises


async def delete_agent_everywhere(*, owner_id: str, agent_id: str) -> dict[str, Any]:
    """Delete an agent across Paperclip and its Hermes profile.

    The profile delete happens before Paperclip removal: if it fails, the Paperclip row stays
    visible and a retry can reconcile the operation.
    """

    from core.dynamodb import run_in_thread
    from core.services import hermes_profiles

    preserved = await prepare_paperclip_agent_delete(owner_id=owner_id, agent_id=agent_id)

    # The S3 profile prefix is this transport's whole durable record of the agent -- its
    # config.yaml, .env and SOUL.md. There is no gateway `agents.list` to probe first, no
    # `agents.delete` RPC, and no openclaw.json entry or Lightbulb channel account to strip.
    await run_in_thread(hermes_profiles.delete_profile, owner_id, agent_id)
    await delete_paperclip_for_chat(owner_id=owner_id, agent_id=agent_id)
    await _refresh_facts(owner_id)  # every caller (RPC, Teams, admin): no survivor keeps a ghost teammate
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
        adapter_type, adapter_config = await resolve_agent_adapter(owner_id)
        fields = resolve_create_agent_fields(role=role, meta=meta)
        idempotency_key = build_agent_create_idempotency_key(
            operation="chat",
            owner_id=owner_id,
            intent=paperclip_agent_intent(
                name=name,
                adapter_type=adapter_type,
                **fields,
            ),
        )
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            adapter_type=adapter_type,
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
    Paperclip (treats already-gone as success), so agents that never existed
    in Paperclip and double-deletes are safe.

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
        await repo.remove_starter_agent_id(owner_id, agent_id)
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
        adapter_type, adapter_config = await resolve_agent_adapter(owner_id)
        fields = resolve_create_agent_fields(role=role, meta=meta)
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            adapter_type=adapter_type,
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
    await _refresh_facts(owner_id)
