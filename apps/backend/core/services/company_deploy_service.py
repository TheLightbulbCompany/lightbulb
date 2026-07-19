"""Company deploy orchestration — instantiate a whole ``format="company"``
package into the owner's Paperclip org + OpenClaw container.

This is the deploy engine's core. Given a company tarball (the companies.sh
``agentcompanies/v1`` format) it walks the parsed :class:`DeployPlan` agent by
agent in topological order and, for each agent, reuses the SAME live-create
machinery the Teams create-agent path uses:

    mint service token -> synthesize openclaw_gateway adapter (server-side) ->
    Paperclip create_agent -> pair_with_openclaw -> ensure_operator_device_paired
    -> materialize_agent (identity + skills onto EFS + openclaw.json entry).

Org-chart grafting: the owner's seeded **Friday** is the real CEO. The package
has its own CEO (``plan.ceo_slug``). Anything at the top of the package graph
(the package CEO itself, or an agent that reports to it — the "division head")
is re-parented to report to Friday; everyone else keeps the package's
``reportsTo`` edge, remapped to the freshly-minted manager id.

Server-initiated: this runs off the request path (Task 5 fires it via
``asyncio.create_task``), so there is NO user browser cookie. The Paperclip
create/list/routine calls authenticate with the **bootstrap-admin** Better Auth
session cookie — the exact mechanism the marketplace server-deploy uses
(``paperclip_agent_lifecycle.create_paperclip_for_marketplace`` +
``marketplace_listings._recreate_routines_for_buyer``): a long-lived
``httpx.AsyncClient`` against ``PAPERCLIP_INTERNAL_URL`` plus
``get_admin_session_cookie``.

``run_company_deploy`` NEVER raises: any failure records ``status="failed"`` on
the job row (with the error string) and returns, leaving already-created agents
in place so the job can be **resumed** (a re-run skips agents already
``materialized``). ``rollback_company_deploy`` is the explicit teardown: it
deletes exactly the agents this job created (guarding ``is_seeded_agent`` so
Friday/Atlas can never be removed) and unwinds their container/EFS side effects.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path

import httpx

from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool, get_workspace
from core.repositories import company_deploy_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import service_token
from core.services.agent_routine_provisioning import create_agent_routines
from core.services.catalog_service import get_catalog_service
from core.services.company_agent_materializer import materialize_agent
from core.services.company_package import DeployPlan, PlannedAgent, parse_deploy_plan
from core.services.config_patcher import apply_deploy_aux_mutations
from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import delete_paperclip_for_marketplace
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    is_seeded_agent,
    pair_with_openclaw,
    resolve_company_id_for_owner,
)
from core.services.paperclip_provisioning import _ws_gateway_url

logger = logging.getLogger(__name__)

# Per-agent monthly spend cap for an imported company agent ($10/mo). Company
# imports fan out to many agents; a bounded per-agent budget keeps a runaway
# import from opening an unbounded number of unbudgeted spenders.
COMPANY_IMPORT_AGENT_BUDGET_CENTS = 1000


# ---------------------------------------------------------------------------
# Small indirection helpers (kept as module-level names so unit tests can
# monkeypatch the security-critical wiring — admin session, adapter synthesis,
# pairing — without the full AWS / Paperclip stack).
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _gateway_url() -> str:
    """WS gateway URL for the running environment (allowlisted by the adapter)."""
    return _ws_gateway_url(settings.ENVIRONMENT or "")


async def _open_admin_session() -> tuple[httpx.AsyncClient, PaperclipAdminClient, str]:
    """Open a Paperclip **bootstrap-admin** session for a server-initiated deploy.

    Reuses the exact mechanism the marketplace server-deploy path uses
    (``create_paperclip_for_marketplace`` / ``_recreate_routines_for_buyer``):
    a long-lived ``httpx.AsyncClient`` against ``PAPERCLIP_INTERNAL_URL`` plus a
    bootstrap-admin Better Auth cookie from ``get_admin_session_cookie`` — NOT a
    user browser cookie (there is none off the request path). The caller owns
    closing the returned client.
    """
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
    except BaseException:
        # Close the just-opened client on a failed sign-in so it never leaks —
        # the exception then propagates to run_company_deploy's failure handler.
        await http.aclose()
        raise
    return http, admin, cookie


def _parse_plan_from_tar(tar_bytes: bytes) -> DeployPlan:
    """Extract the company tarball to a temp dir and parse the deploy plan.

    Tolerates a tarball packed with a single top-level directory (the common
    ``add(pkg_dir, arcname=pkg_dir.name)`` shape) by descending to the dir that
    actually holds ``COMPANY.md``. All file reads happen inside the temp-dir
    context (``parse_deploy_plan`` reads bodies into memory), so the returned
    plan is safe to use after cleanup.
    """
    with tempfile.TemporaryDirectory(prefix="company-deploy-") as tmp:
        base = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            tf.extractall(base, filter="data")
        return parse_deploy_plan(_find_package_root(base))


def _find_package_root(base: Path) -> Path:
    """Locate the directory containing ``COMPANY.md`` (base or a single subdir)."""
    if (base / "COMPANY.md").is_file():
        return base
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "COMPANY.md").is_file():
            return child
    return base  # let parse_deploy_plan raise the clear "no COMPANY.md" error


async def _parse_plan_async(tar_bytes: bytes) -> DeployPlan:
    """Off-loop wrapper: tar extraction + file reads must not stall the loop."""
    return await asyncio.to_thread(_parse_plan_from_tar, tar_bytes)


def _find_friday_id(agents) -> str | None:
    """Friday = the company's CEO agent (``role == "ceo"``).

    Accepts either the raw list the admin client returns or an enveloped
    ``{"agents": [...]}`` shape defensively. Returns the first ``ceo`` match;
    Friday is seeded at provision time (long before any company deploy) so it is
    the first ``ceo`` in creation-ordered rosters.
    """
    if isinstance(agents, dict):
        agents = agents.get("agents") or agents.get("items") or []
    if not isinstance(agents, list):
        return None
    for a in agents:
        if isinstance(a, dict) and a.get("role") == "ceo":
            return a.get("id")
    return None


def _resolve_reports_to(planned: PlannedAgent, plan: DeployPlan, friday_id: str, slug_to_id: dict[str, str]) -> str:
    """Resolve one agent's Paperclip ``reports_to`` id.

    The package's top graft onto Friday (the owner's real CEO): the package CEO
    itself, any agent that reports to it, and any other rootless agent. Every
    other agent keeps the package's edge, remapped to the manager's
    freshly-minted id. A manager not yet in the map (only possible under a
    parse-time cycle, which ``_topo_order`` appends by slug) falls back to
    Friday so the agent still attaches to the org.
    """
    rts = planned.reports_to_slug
    if planned.slug == plan.ceo_slug or rts is None or rts == plan.ceo_slug:
        return friday_id
    return slug_to_id.get(rts) or friday_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_company_deploy(*, job_id: str, owner_id: str, listing: dict, tar_bytes: bytes) -> None:
    """Deploy a whole company package. Background target — NEVER raises.

    On any exception the job row is flipped to ``failed`` (with the error
    string) and already-created agents are left in place for a resume. See the
    module docstring for the full recipe.
    """
    try:
        await _run_company_deploy(job_id=job_id, owner_id=owner_id, tar_bytes=tar_bytes)
    except Exception as exc:  # noqa: BLE001 — background target must never escape
        logger.exception("company_deploy: job=%s owner=%s failed", job_id, owner_id)
        try:
            await company_deploy_repo.set_status(job_id=job_id, status="failed", now_iso=_now_iso(), error=str(exc))
        except Exception:
            logger.exception("company_deploy: also failed to record failed status job=%s", job_id)


async def _run_company_deploy(*, job_id: str, owner_id: str, tar_bytes: bytes) -> None:
    """Inner orchestration (may raise — the public wrapper records failure)."""
    plan = await _parse_plan_async(tar_bytes)

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        await company_deploy_repo.set_status(
            job_id=job_id,
            status="failed",
            now_iso=_now_iso(),
            error="owner has no Paperclip company",
        )
        return

    # Container reachability — an unreachable container fails the job BEFORE any
    # create, so we never orphan Paperclip agents against a container that can't
    # pair them.
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception:  # noqa: BLE001 — treat any ECS/DDB error as unreachable
        container, ip = None, None
    gateway_token = container.get("gateway_token") if isinstance(container, dict) else None
    if not container or not ip or not gateway_token:
        await company_deploy_repo.set_status(
            job_id=job_id,
            status="failed",
            now_iso=_now_iso(),
            error="container unreachable",
        )
        return

    http, admin, cookie = await _open_admin_session()
    try:
        friday_id = _find_friday_id(await admin.list_agents(session_cookie=cookie, company_id=company_id))
        if not friday_id:
            await company_deploy_repo.set_status(
                job_id=job_id,
                status="failed",
                now_iso=_now_iso(),
                error="Friday (role=ceo) not found in company",
            )
            return

        # Resume: seed the slug->id map with agents a prior run already
        # materialized, and skip re-creating them below.
        job = await company_deploy_repo.get_job(job_id)
        already: dict[str, str] = {}
        for entry in job.get("agents", []) if job else []:
            if entry.get("state") == "materialized" and entry.get("paperclip_id"):
                already[entry["slug"]] = entry["paperclip_id"]

        slug_to_id: dict[str, str] = dict(already)
        gateway_pool = get_gateway_pool()
        workspace = get_workspace()

        for planned in plan.agents:
            if planned.slug in already:
                continue  # resume-skip: fully materialized in a prior run

            reports_to = _resolve_reports_to(planned, plan, friday_id, slug_to_id)

            # Adapter is ALWAYS synthesized server-side — a PlannedAgent carries
            # no adapter, so nothing package-supplied can reach Paperclip.
            adapter_config = synthesize_openclaw_adapter(
                gateway_url=_gateway_url(),
                service_token=service_token.mint(owner_id),
                user_id=owner_id,
            )
            pc = await admin.create_agent(
                session_cookie=cookie,
                company_id=company_id,
                name=planned.name,
                role=planned.role,
                adapter_type=OPENCLAW_GATEWAY_TYPE,
                adapter_config=adapter_config,
                title=planned.title,
                capabilities=planned.capabilities,
                reports_to=reports_to,
                budget_monthly_cents=COMPANY_IMPORT_AGENT_BUDGET_CENTS,
            )
            pc_id = pc["id"]
            slug_to_id[planned.slug] = pc_id
            await company_deploy_repo.record_agent(
                job_id=job_id, slug=planned.slug, paperclip_id=pc_id, state="created", now_iso=_now_iso()
            )

            # Register the agent in the running container + trust its operator
            # device, exactly like the /teams create path.
            await pair_with_openclaw(
                owner_id=owner_id,
                paperclip_agent=pc,
                gateway_pool=gateway_pool,
                ip=ip,
                token=gateway_token,
            )
            await ensure_operator_device_paired(owner_id=owner_id, paperclip_agent=pc, gateway_token=gateway_token)
            await company_deploy_repo.record_agent(
                job_id=job_id, slug=planned.slug, paperclip_id=pc_id, state="paired", now_iso=_now_iso()
            )

            # Land identity + skill bindings onto EFS + openclaw.json. Company
            # agents are peers in one owner-authored org, so the materializer is
            # delegation-preserving (no single-agent containment clamp).
            await materialize_agent(
                owner_id=owner_id,
                new_agent_id=pc_id,
                planned=planned,
                skill_assets=None,
                workspace=workspace,
                apply_aux=apply_deploy_aux_mutations,
            )
            await company_deploy_repo.record_agent(
                job_id=job_id, slug=planned.slug, paperclip_id=pc_id, state="materialized", now_iso=_now_iso()
            )

        await _create_routines(admin=admin, cookie=cookie, company_id=company_id, plan=plan, slug_to_id=slug_to_id)

        await company_deploy_repo.set_status(job_id=job_id, status="completed", now_iso=_now_iso())
    finally:
        await http.aclose()


async def _create_routines(
    *, admin, cookie: str, company_id: str, plan: DeployPlan, slug_to_id: dict[str, str]
) -> None:
    """Create each planned routine DORMANT (paused) on its assigned agent.

    ``create_agent_routines`` always attaches an ``enabled: False`` schedule
    trigger, so nothing fires until a human flips it on. Routines are grouped by
    assignee id; cron-less specs are dropped (a schedule trigger needs a cron),
    and routines whose assignee never got an id (skipped/failed agent) are too.
    """
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for routine in plan.routines:
        if not routine.cron:
            continue
        agent_id = slug_to_id.get(routine.agent_slug)
        if not agent_id:
            continue
        by_agent[agent_id].append({"title": routine.title, "description": routine.description, "cron": routine.cron})

    for agent_id, specs in by_agent.items():
        await create_agent_routines(
            admin=admin,
            session_cookie=cookie,
            company_id=company_id,
            agent_id=agent_id,
            routines=specs,
        )


async def rollback_company_deploy(*, job_id: str) -> None:
    """Tear down exactly the agents this job created.

    For each recorded agent that has a ``paperclip_id``: guard ``is_seeded_agent``
    (fails CLOSED — a seeded Friday/Atlas is never deleted), then delete the
    Paperclip agent and unwind its container/EFS side effects. Per-agent
    failures are logged, never raised, so one stuck delete can't strand the
    rest. Finally flips the job to ``rolled_back``.
    """
    job = await company_deploy_repo.get_job(job_id)
    if job is None:
        logger.warning("rollback_company_deploy: job %s not found", job_id)
        return
    owner_id = job.get("owner_id")
    catalog = get_catalog_service()

    for entry in job.get("agents", []):
        pc_id = entry.get("paperclip_id")
        if not pc_id:
            continue
        if await is_seeded_agent(owner_id=owner_id, agent_id=pc_id):
            logger.warning("rollback_company_deploy: refusing to delete seeded agent %s (owner=%s)", pc_id, owner_id)
            continue
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=pc_id)
        except Exception:
            logger.exception("rollback_company_deploy: delete failed for agent=%s", pc_id)
        try:
            await catalog.unwind_partial_deploy(owner_id=owner_id, new_agent_id=pc_id)
        except Exception:
            logger.exception("rollback_company_deploy: unwind failed for agent=%s", pc_id)

    await company_deploy_repo.set_status(job_id=job_id, status="rolled_back", now_iso=_now_iso())
