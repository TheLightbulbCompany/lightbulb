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

Fire-and-forget: the deploy endpoint fires this via ``asyncio.create_task`` and
returns 202 immediately, so there is NO job row and NO user browser cookie. The
Paperclip create/list/routine calls authenticate with the **bootstrap-admin**
Better Auth session cookie — the exact mechanism the marketplace server-deploy
uses (``paperclip_agent_lifecycle.create_paperclip_for_marketplace`` +
``marketplace_listings._recreate_routines_for_buyer``): a long-lived
``httpx.AsyncClient`` against ``PAPERCLIP_INTERNAL_URL`` plus
``get_admin_session_cookie``.

``run_company_deploy`` NEVER raises (it is a background task target). It tracks
the agents it creates in a local list; on any failure it best-effort rolls back
exactly that set (deleting each Paperclip agent + unwinding its container/EFS
side effects, guarding ``is_seeded_agent`` so Friday/Atlas can never be removed)
and logs. Progress and the final roster are observed in Teams — the Paperclip
agent roster is the source of truth; this flow just kicks off + cleans up.
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
from core.repositories import container_repo
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
    """Friday = the owner's true apex CEO: the ``role == "ceo"`` agent that is
    ROOTLESS (empty ``reportsTo``).

    After a company deploy the package's own CEO exists too, as a division head
    with ``reportsTo = friday_id`` — so the company has 2+ ``role == "ceo"``
    agents and "first ceo" is unsafe: ``list_agents`` order is not contractually
    Friday-first, so a SECOND deploy could otherwise graft the new org chart onto
    a prior package's CEO. Friday is the only ceo with no manager, so select on
    that instead. Paperclip returns the field as ``reportsTo`` (camelCase — see
    the create body in ``paperclip_admin_client``); ``reports_to`` is accepted
    too for defensiveness.

    Accepts the raw list or an enveloped ``{"agents": [...]}`` shape. Falls back
    to the first ceo if (impossibly) none are rootless, so a present ceo never
    yields ``None``.
    """
    if isinstance(agents, dict):
        agents = agents.get("agents") or agents.get("items") or []
    if not isinstance(agents, list):
        return None
    first_ceo: str | None = None
    for a in agents:
        if not isinstance(a, dict) or a.get("role") != "ceo":
            continue
        if first_ceo is None:
            first_ceo = a.get("id")
        reports_to = a.get("reportsTo")
        if reports_to is None:
            reports_to = a.get("reports_to")
        if not reports_to:  # rootless apex = the owner's seeded Friday
            return a.get("id")
    return first_ceo


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


async def run_company_deploy(*, owner_id: str, listing: dict, tar_bytes: bytes, claim_token: str | None = None) -> None:
    """Deploy a whole company package. Background target — NEVER raises.

    Fire-and-forget: the deploy endpoint has already returned 202. On any
    failure the agents this run created are best-effort rolled back (see
    ``_rollback``) and the error logged; the roster and progress are observed in
    Teams. See the module docstring for the full recipe.

    ``claim_token`` — the owner-wide deploy claim the router took before
    dispatching. This run owns it: the lease is extended per agent (see
    ``_run_company_deploy``) and released here when the run settles. The
    release passes the failure as ``cause`` so a pairing ``TimeoutError``
    HOLDS the claim — the container may still be writing — exactly like the
    single-agent path.
    """
    created: list[dict] = []
    failure: BaseException | None = None
    try:
        await _run_company_deploy(owner_id=owner_id, tar_bytes=tar_bytes, created=created, claim_token=claim_token)
    except Exception as exc:  # noqa: BLE001 — background target must never escape
        failure = exc
        logger.exception("company_deploy: owner=%s failed — rolling back %d created agent(s)", owner_id, len(created))
        # cause=exc lets unwind_partial_deploy skip the EFS/openclaw.json edit
        # on a client-side TimeoutError (pair_with_openclaw can time out) —
        # see its docstring.
        await _rollback(owner_id, created, cause=exc)
    finally:
        if claim_token:
            await container_repo.release_owner_deploy_claim(
                owner_id,
                listing_id=str(listing.get("listing_id")),
                token=claim_token,
                cause=failure,
            )


async def _run_company_deploy(
    *, owner_id: str, tar_bytes: bytes, created: list[dict], claim_token: str | None = None
) -> None:
    """Inner orchestration (may raise — the public wrapper rolls back + swallows).

    Appends each freshly-created Paperclip agent dict to ``created`` as it is
    made, so the wrapper's failure path can tear down exactly this run's agents.
    """
    plan = await _parse_plan_async(tar_bytes)

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.warning("company_deploy: owner=%s has no Paperclip company — creating nothing", owner_id)
        return

    # Container reachability — an unreachable container means we create NOTHING,
    # so we never orphan Paperclip agents against a container that can't pair
    # them.
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception:  # noqa: BLE001 — treat any ECS/DDB error as unreachable
        container, ip = None, None
    gateway_token = container.get("gateway_token") if isinstance(container, dict) else None
    if not container or not ip or not gateway_token:
        logger.warning("company_deploy: owner=%s container unreachable — creating nothing", owner_id)
        return

    http, admin, cookie = await _open_admin_session()
    try:
        friday_id = _find_friday_id(await admin.list_agents(session_cookie=cookie, company_id=company_id))
        if not friday_id:
            logger.warning("company_deploy: owner=%s Friday (role=ceo) not found — creating nothing", owner_id)
            return

        slug_to_id: dict[str, str] = {}
        gateway_pool = get_gateway_pool()
        workspace = get_workspace()

        for planned in plan.agents:
            # Keep the owner-wide deploy lease live across the whole run: each
            # per-agent step is bounded well under DEPLOY_CLAIM_STALE_S, but N
            # of them are not. A dead run stops extending and is taken over on
            # the normal cutoff. Never raises.
            if claim_token:
                await container_repo.extend_owner_deploy_claim(owner_id, token=claim_token)

            reports_to = _resolve_reports_to(planned, plan, friday_id, slug_to_id)

            # Adapter is ALWAYS synthesized server-side — a PlannedAgent carries
            # no adapter, so nothing package-supplied can reach Paperclip.
            adapter_config = synthesize_openclaw_adapter(
                gateway_url=_gateway_url(),
                service_token=service_token.mint(owner_id),
                user_id=owner_id,
            )
            # Deliberately omit agent-create idempotency here. The owner-wide
            # deploy claim serializes whole runs, but nothing REPLAYS a
            # half-finished run per-agent — a failed run rolls back its own
            # agents and a retry re-mints from scratch, so a replayed agent id
            # has nothing durable to key on.
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
            # Track the agent BEFORE pairing/materialize so a failure in either
            # rolls back the agent that was already created.
            created.append(pc)

            # Register the agent in the running container + trust its operator
            # device, exactly like the /teams create path.
            await pair_with_openclaw(
                owner_id=owner_id,
                paperclip_agent=pc,
                gateway_pool=gateway_pool,
                ip=ip,
                token=gateway_token,
            )
            await ensure_operator_device_paired(
                owner_id=owner_id, paperclip_agent=pc, gateway_token=gateway_token, ip=ip
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

        # Routine creation is OUTSIDE the rollback-triggering path. Once every
        # agent is materialized the deploy is a success; routines are created
        # PAUSED and are freely re-addable from Teams, so a routine hiccup must
        # NOT tear down a healthy, fully-deployed company. Log it and move on —
        # the user adds/enables routines from Teams.
        try:
            await _create_routines(admin=admin, cookie=cookie, company_id=company_id, plan=plan, slug_to_id=slug_to_id)
        except Exception:  # noqa: BLE001 — a routine failure never rolls back a materialized company
            logger.exception(
                "company_deploy: owner=%s routine creation failed after all agents materialized "
                "— leaving the deployed company intact (add routines from Teams)",
                owner_id,
            )
    finally:
        await http.aclose()


async def _create_routines(
    *, admin, cookie: str, company_id: str, plan: DeployPlan, slug_to_id: dict[str, str]
) -> None:
    """Create each planned routine DORMANT (paused) on its assigned agent.

    ``create_agent_routines`` always attaches an ``enabled: False`` schedule
    trigger, so nothing fires until a human flips it on from Teams. Routines are
    grouped by assignee id; cron-less specs are dropped (a schedule trigger
    needs a cron), and routines whose assignee never got an id (a
    skipped/failed agent) are too.
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


async def _rollback(owner_id: str, created: list[dict], *, cause: BaseException | None = None) -> None:
    """Best-effort teardown of exactly the agents this run created.

    For each created agent: guard ``is_seeded_agent`` (fails CLOSED — a seeded
    Friday/Atlas is never deleted; if the check itself errors we SKIP rather than
    risk deleting a seed), then delete the Paperclip agent and unwind its
    container/EFS side effects. Each step is in its own try/except so one stuck
    delete can't strand the rest. Never raises.

    ``cause`` is threaded into ``unwind_partial_deploy`` so it can skip the
    EFS/openclaw.json edit on a client-side TimeoutError — see its docstring.
    It applies to the LAST created agent ONLY. ``created`` is appended to right
    after ``create_agent`` and before that agent is paired/materialized, so the
    only agent the container can still be mid-write on is ``created[-1]``;
    everything before it finished its RPCs. Applying one agent's TimeoutError to
    the whole list is what turned a 10-agent deploy whose 10th agent timed out
    into nine permanently-stranded EFS workspaces + openclaw.json entries, while
    their Paperclip rows were deleted anyway — nine unreachable ghosts where the
    pre-branch behaviour unwound all ten.
    """
    catalog = get_catalog_service()
    last_id = next((pc.get("id") if isinstance(pc, dict) else pc for pc in reversed(created)), None)
    for pc in created:
        pc_id = pc.get("id") if isinstance(pc, dict) else pc
        if not pc_id:
            continue
        # Only the agent the failure landed on gets the timeout carve-out; the
        # rest are known-complete and must be unwound normally.
        agent_cause = cause if pc_id == last_id else None
        try:
            if await is_seeded_agent(owner_id=owner_id, agent_id=pc_id):
                logger.warning(
                    "company_deploy rollback: refusing to delete seeded agent %s (owner=%s)", pc_id, owner_id
                )
                continue
        except Exception:
            logger.exception("company_deploy rollback: is_seeded_agent check failed for agent=%s — skipping", pc_id)
            continue
        # Container side first: when the unwind is SKIPPED (timeout) the install
        # is still standing, so the Paperclip row has to stay with it — deleting
        # it alone leaves an agent nobody can see or remove from Teams.
        try:
            unwound = await catalog.unwind_partial_deploy(owner_id=owner_id, new_agent_id=pc_id, cause=agent_cause)
        except Exception:
            # ``unwind_partial_deploy`` is documented never to raise, so this is
            # the unknown-unknown branch. Treat it as a FAILED unwind: the
            # container may still hold the workspace, the agents.list entry and
            # the sidecar, and dropping the Paperclip row on top of that is what
            # manufactures an unreachable ghost. A retained row is deletable from
            # Teams; a ghost is not.
            logger.exception("company_deploy rollback: unwind failed for agent=%s", pc_id)
            unwound = False
        if not unwound:
            logger.warning(
                "company_deploy rollback: keeping agent=%s (owner=%s) — container-side install was NOT "
                "safely unwound (client-side timeout, the sidecar unlink failed, or the unwind itself "
                "raised), so it stays visible + deletable in Teams",
                pc_id,
                owner_id,
            )
            continue
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=pc_id)
        except Exception:
            logger.exception("company_deploy rollback: delete failed for agent=%s", pc_id)
