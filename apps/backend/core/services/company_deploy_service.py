"""Company deploy orchestration — instantiate a whole ``format="company"``
package into the owner's Paperclip org + OpenClaw container.

This is the deploy engine's core. Given a company tarball (the companies.sh
``agentcompanies/v1`` format) it walks the parsed :class:`DeployPlan` agent by
agent in topological order and, for each agent, reuses the SAME live-create
machinery the Teams create-agent path uses:

    mint service token -> synthesize openclaw_gateway adapter (server-side) ->
    Paperclip create_agent
    -> write_profile_for_agent (config.yaml/.env/SOUL.md + AGENTS.md into the S3 profile).

Org-chart grafting: the owner's root **CEO** is the real CEO. The package
has its own CEO (``plan.ceo_slug``). Anything at the top of the package graph
(the package CEO itself, or an agent that reports to it — the "division head")
is re-parented to report to that root CEO; everyone else keeps the package's
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
side effects, guarding legacy seeded agents so they can never be removed)
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
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_agent_materializer import render_agent_identity_md
from core.services.company_package import DeployPlan, PlannedAgent, parse_deploy_plan
from core.services.hermes_profiles import upload_profile_files
from core.services.hermes_provisioning import resolve_profile_inputs, write_profile_for_agent
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import delete_paperclip_for_marketplace, resolve_agent_adapter
from core.services.paperclip_openclaw_pairing import (
    is_seeded_agent,
    resolve_company_id_for_owner,
)

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

    On real success (agents were created) the owner's ``deployed_company``
    marker is written — the durable half of the ONE-company-per-workspace
    fence the deploy route checks.
    """
    created: list[dict] = []
    run_state: dict = {}
    failure: BaseException | None = None
    try:
        await _run_company_deploy(
            owner_id=owner_id, tar_bytes=tar_bytes, created=created, claim_token=claim_token, run_state=run_state
        )
    except Exception as exc:  # noqa: BLE001 — background target must never escape
        failure = exc
        logger.exception("company_deploy: owner=%s failed — rolling back %d created agent(s)", owner_id, len(created))
        # Delete the Paperclip agent only: there is no EFS/openclaw.json state to unwind, and
        # S3 profile objects are left for a re-run to overwrite.
        await _rollback(owner_id, created)
    else:
        # REAL success only — `created` is empty on the creating-nothing early
        # returns (no company / container unreachable / no Friday), which must
        # not burn the owner's one company slot. Never raises.
        if created:
            await container_repo.set_deployed_company(
                owner_id,
                listing_id=str(listing.get("listing_id") or ""),
                name=str(listing.get("name") or ""),
            )
    finally:
        if claim_token:
            await container_repo.release_owner_deploy_claim(
                owner_id,
                listing_id=str(listing.get("listing_id")),
                token=claim_token,
                cause=failure,
            )


async def _run_company_deploy(
    *,
    owner_id: str,
    tar_bytes: bytes,
    created: list[dict],
    claim_token: str | None = None,
    run_state: dict | None = None,
) -> None:
    """Inner orchestration (may raise — the public wrapper rolls back + swallows).

    Appends each freshly-created Paperclip agent dict to ``created`` as it is
    made, so the wrapper's failure path can tear down exactly this run's agents.

    ``run_state`` is accepted and unused: it carried the ``is_agentcore`` flag the wrapper read
    after a failure to pick between two rollback postures, and only one posture remains.
    """
    plan = await _parse_plan_async(tar_bytes)

    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.warning("company_deploy: owner=%s has no Paperclip company — creating nothing", owner_id)
        return

    # Readiness — an unprovisioned workspace means we create NOTHING, so we never orphan
    # Paperclip agents against a runtime that cannot serve them. Any InvokeAgentRuntime call
    # wakes the microVM (M4 Task 22), so there is nothing to discover or wake first: the row
    # existing with a minted runtime ARN IS "reachable".
    try:
        container = await container_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — treat any DDB error as unreachable
        container = None
    if not container or not container.get("agentcore_runtime_arn"):
        logger.warning("company_deploy: owner=%s has no provisioned runtime yet — creating nothing", owner_id)
        return

    http, admin, cookie = await _open_admin_session()
    try:
        friday_id = _find_friday_id(await admin.list_agents(session_cookie=cookie, company_id=company_id))
        if not friday_id:
            logger.warning("company_deploy: owner=%s Friday (role=ceo) not found — creating nothing", owner_id)
            return

        slug_to_id: dict[str, str] = {}
        # Owner-level profile inputs (billing/Secrets/integrations/env) are resolved ONCE for
        # the whole roster, mirroring hermes_provisioning.write_profiles's own fan-out — never
        # once per agent.
        profile_inputs = await resolve_profile_inputs(owner_id)
        skipped_aux: dict[str, int] = defaultdict(int)

        for planned in plan.agents:
            # Keep the owner-wide deploy lease live across the whole run: each
            # per-agent step is bounded well under DEPLOY_CLAIM_STALE_S, but N
            # of them are not. A dead run stops extending and is taken over on
            # the normal cutoff. Never raises.
            if claim_token:
                await container_repo.extend_owner_deploy_claim(owner_id, token=claim_token)

            reports_to = _resolve_reports_to(planned, plan, friday_id, slug_to_id)

            # Adapter is ALWAYS resolved server-side — a PlannedAgent carries no adapter, so
            # nothing package-supplied can reach Paperclip. Through the one shared resolver
            # every Paperclip agent-creation door uses.
            adapter_type, adapter_config = await resolve_agent_adapter(owner_id)
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
                adapter_type=adapter_type,
                adapter_config=adapter_config,
                title=planned.title,
                capabilities=planned.capabilities,
                reports_to=reports_to,
                budget_monthly_cents=COMPANY_IMPORT_AGENT_BUDGET_CENTS,
            )
            pc_id = pc["id"]
            slug_to_id[planned.slug] = pc_id
            # Track the agent BEFORE the profile write so a failure there rolls back the
            # agent that was already created.
            created.append(pc)

            # The Hermes profile IS the pairing: write_profile_for_agent mints this agent's own
            # Paperclip key and writes config.yaml/.env/SOUL.md; the package's own per-agent
            # content (today: just AGENTS.md) rides in a second small upload ending in its own
            # .revision.
            await write_profile_for_agent(owner_id, pc, inputs=profile_inputs, publish=False)
            await upload_profile_files(owner_id, pc_id, {"AGENTS.md": render_agent_identity_md(planned).encode()})
            # apply_deploy_aux_mutations (the agents.list entry -- including the planner's
            # skills binding -- and the paperclip-creds plugin enable) has no Hermes equivalent:
            # a Hermes profile has no per-agent agents.list, and its .env already carries
            # PAPERCLIP_API_KEY directly (written above). Log and count each kind for the
            # deploy summary below.
            for kind in ("agent_entry", "plugins_patch"):
                skipped_aux[kind] += 1
                logger.info(
                    "company_deploy: owner=%s agent=%s skipped aux mutation kind=%s (no Hermes equivalent)",
                    owner_id,
                    pc_id,
                    kind,
                )

        # Per-agent writes above share one `inputs` and never refresh each other's `teammates`.
        from core.services.owner_facts import write_owner_facts

        await write_owner_facts(owner_id)  # best-effort: keeps the agents' facts manifest current
        from core.services import network_member

        await network_member.publish_sessions(owner_id)  # once, after the per-agent writes

        logger.info(
            "company_deploy: owner=%s deploy summary -- created=%d skipped_aux=%s",
            owner_id,
            len(created),
            dict(skipped_aux),
        )

    finally:
        await http.aclose()


async def _rollback(
    owner_id: str, created: list[dict], *, cause: BaseException | None = None, is_agentcore: bool = True
) -> None:
    """Best-effort teardown of exactly the agents this run created.

    For each created agent: guard ``is_seeded_agent`` (fails CLOSED — a seeded legacy founder
    seed is never deleted; if the check itself errors we skip rather than risk deleting it),
    then delete the Paperclip agent. Each step is in its own try/except so one stuck delete
    can't strand the rest. Never raises.

    Nothing else to unwind: the per-agent loop writes only S3 profile objects, which are left in
    place because a re-run overwrites them (M6-R33). ``cause``/``is_agentcore`` are accepted and
    unused — the ECS unwind they steered (``catalog.unwind_partial_deploy``'s EFS +
    openclaw.json edit, skipped on a client-side TimeoutError) has no equivalent here.
    """
    for pc in created:
        pc_id = pc.get("id") if isinstance(pc, dict) else pc
        if not pc_id:
            continue
        try:
            if await is_seeded_agent(owner_id=owner_id, agent_id=pc_id):
                logger.warning(
                    "company_deploy rollback: refusing to delete seeded agent %s (owner=%s)", pc_id, owner_id
                )
                continue
        except Exception:
            logger.exception("company_deploy rollback: is_seeded_agent check failed for agent=%s — skipping", pc_id)
            continue
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=pc_id)
        except Exception:
            logger.exception("company_deploy rollback: delete failed for agent=%s", pc_id)
