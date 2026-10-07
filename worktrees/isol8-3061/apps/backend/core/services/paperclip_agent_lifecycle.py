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
from core.repositories import billing_repo, container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient
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


async def repoint_agent_adapter(owner_id: str, agent: dict[str, Any]) -> bool:
    """Move ONE **existing** agent's Paperclip adapter onto the native Hermes transport.

    ``resolve_agent_adapter`` above picks the adapter at agent-CREATE time and nothing had ever
    re-picked it, so a workspace that MOVED between runtimes kept whatever its agents were
    created with (M6 task-14 row 1, the founder blocker): every migrated workspace's agents went
    on dialling an operator WebSocket at an address that no longer resolves, so each routine
    fire, issue run and Teams Run/Retry died while room chat -- which rides the WORK bus, not the
    adapter -- kept working. Nothing looked broken.

    Deliberately the SAME resolver a fresh create uses.

    ZERO CALLERS TODAY -- the migration job that drove it is deleted, and every prod and dev
    workspace is migrated. Kept because it is the only repair path for an agent row still
    carrying ``openclaw_gateway``: that agent's routines, issue runs and Teams Run/Retry all
    fail silently while room chat (which rides the WORK bus, not the adapter) keeps working, so
    nothing looks broken. Re-deriving it from scratch mid-incident is the expensive option.

    Returns True when a patch landed, False when the agent was already on the target adapter --
    i.e. idempotent, so a re-entered migration tail re-points only what is still wrong.
    **Raises** on any failure: a half-re-pointed workspace looks healthy, which is the entire
    defect this closes.
    """
    agent_id = agent.get("id")
    if not agent_id:
        raise ValueError(f"repoint_agent_adapter: agent row for owner={owner_id} has no id")
    if agent.get("adapterType") == HERMES_GATEWAY_TYPE:
        return False
    # The empty-hook-token guard lives INSIDE build_hermes_gateway_adapter_config, which raises
    # AdapterConfigError before the PATCH: every door onto the native adapter (seed, /teams
    # create, this re-point) gets the same refusal.
    _, adapter_config = await resolve_agent_adapter(owner_id)
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        await admin.patch_agent(
            session_cookie=cookie,
            agent_id=agent_id,
            body={
                "adapterType": HERMES_GATEWAY_TYPE,
                "adapterConfig": adapter_config,
                "replaceAdapterConfig": True,
            },
        )
    logger.info("repoint_agent_adapter: owner=%s agent=%s -> %s", owner_id, agent_id, HERMES_GATEWAY_TYPE)
    return True


async def list_hermes_gateway_agents(owner_id: str) -> tuple[str | None, list[str]]:
    """The owner's active Paperclip company id and its ``hermes_gateway`` agent ids -- what
    ``sync_agent_adapter_tokens`` would patch. ``(None, [])`` when no company exists yet."""
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


async def sync_agent_adapter_tokens(owner_id: str) -> int:
    """Re-snapshot the row's CURRENT ``hook_token`` into every ``hermes_gateway`` agent's apiKey.

    The adapter config is a snapshot (Paperclip stores the apiKey as a managed company secret),
    and ``provision_org`` short-circuits on an already-active company -- so when an owner's
    containers row is re-created and ``prepare_hermes_workspace`` mints a new hook_token, the
    surviving company's agents keep the OLD one and every run 401s at the facade while room
    chat keeps working. ``repoint_agent_adapter`` skips agents already on ``hermes_gateway``,
    so it cannot fix this; ``openclaw_gateway`` agents stay its job.

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
    logger.info("sync_agent_adapter_tokens: owner=%s company=%s patched=%d", owner_id, company_id, len(agent_ids))
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


async def disarm_owner_nightly_automation(owner_id: str) -> dict[str, int]:
    """Pause a de-entitled owner's nightly CEO routine and cancel the
    in-progress issues that would retry-loop against their deleted container.

    Called from entitlement teardown after the container is gone. The Paperclip
    scheduler fires a routine while ``routines.status='active'`` (no
    company-disabled gate — fork ``tickScheduledTriggers``) and its heartbeat
    runner retry-loops ``in_progress`` issues, so a cancelled owner whose
    routine stays active reproduces the storm where every fire times out on the
    dead container. Pauses the routine (the owner's chosen disarm) and cancels
    the looping issues.

    RE-SUBSCRIBE: pausing self-heals on re-subscribe now that the arm path
    reconciles a paused routine (``create_enabled_schedule_routine`` with
    ``reconcile_paused=True``, re-enabling instead of title-skipping — safe
    because an owner opt-out is filtered before the arm ever reaches the create).
    Two arm paths reach it: the executor (container torn down, ``trust_cache=
    False``) always does, and the poll-path self-heal (container survived a
    best-effort ``delete_user_service``, ``trust_cache=True``) does BECAUSE this
    function releases the arm cache below — otherwise its
    ``nightly_ceo_armed_company`` short-circuit would return ``already_armed``
    and never reach the reconcile. (Archiving instead of pausing would also
    self-heal via the title-skip's ``archived`` exclusion, but it discards this
    routine's run history and config; pausing keeps them, which is why it is the
    chosen disarm.)

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

        # Release the arm cache so a resubscribe's poll-path re-arm reaches the
        # reconcile. arm_nightly_ceo short-circuits on
        # nightly_ceo_armed_company == company_id (trust_cache=True, the poll
        # path), so leaving it set after a pause means a churn→resubscribe owner
        # whose container SURVIVED teardown (delete_user_service is best-effort
        # and swallowed) never re-runs the arm — reconcile_paused never fires and
        # the routine stays paused. The executor path (container torn down,
        # trust_cache=False) ignores the cache and is unaffected either way.
        # release_nightly_arm is a conditional REMOVE (no-op unless it still
        # equals company_id), and this is guarded on its own: the routine pause
        # and issue cancel above are the storm-stoppers and must never be undone
        # by a billing-write hiccup.
        try:
            await billing_repo.release_nightly_arm(owner_id, company_id)
        except Exception:
            logger.exception(
                "paperclip nightly disarm: arm-cache release failed owner=%s company=%s",
                owner_id,
                company_id,
            )

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
