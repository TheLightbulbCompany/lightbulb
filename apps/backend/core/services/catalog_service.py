"""Catalog service — deploy + already-deployed listing.

Publish + unpublish moved to the marketplace flow (see
``routers.marketplace_listings`` + ``core.services.marketplace_service``).
What's left here:
  - ``deploy_from_artifact``: extracts a tarball into the buyer's EFS
    workspace, registers the agent in the container's in-memory state
    via the OpenClaw gateway's ``agents.create`` / ``agents.update`` RPCs,
    and deep-merges the rest of the openclaw-slice (plugins / mcp /
    skills) into ``openclaw.json``. Called by
    /marketplace/listings/{slug}/deploy.
  - ``list_deployed_for_user``: scans the buyer's EFS for ``.template``
    sidecars to drive the chat sidebar's already-deployed filter. Called
    by /marketplace/me/installed-templates.

Why the agent entry goes through RPC and not the file-merge path: chokidar
on EFS polls slowly (1-30s), so writing the agent into ``openclaw.json``
and waiting for the container to pick it up was racy — buyers could hit
a freshly-deployed agent before the runtime knew about it. The gateway
RPC updates the container's in-memory state synchronously.

Depends on injected collaborators so unit tests can mock them:
  - workspace: Workspace (from core.containers.workspace)
  - apply_deploy_aux_mutations: async callable
    (from core.services.config_patcher)
  - pair_fn: async callable
    (from core.services.paperclip_openclaw_pairing.pair_with_openclaw)
  - gateway_pool_factory: callable returning the gateway pool singleton;
    lazy-imported by default so import-time test bootstrap doesn't drag
    in the whole containers module.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


# Mirrors agent_export._AGENT_DIR_SENTINEL. Duplicated rather than
# imported because catalog_service is on the deploy side and shouldn't
# need to depend on agent_export's symbol surface. If a third caller
# emerges, move to a shared constants module.
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"


def _substitute_agent_dir_sentinel(mcp_patch: dict | None, new_agent_id: str) -> dict | None:
    """In-place substitute ``__AGENT_DIR__`` → ``new_agent_id`` in any
    ``args`` entry of every mcp.servers.X dict. Returns the patch
    unchanged if it's None or non-dict.
    """
    if not isinstance(mcp_patch, dict):
        return mcp_patch
    servers = mcp_patch.get("servers")
    if not isinstance(servers, dict):
        return mcp_patch
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [a.replace(_AGENT_DIR_SENTINEL, new_agent_id) if isinstance(a, str) else a for a in args]
    return mcp_patch


def _remap_cron_jobs_for_deploy(
    template_jobs: list[dict],
    *,
    new_agent_id: str,
    owner_id: str,
) -> list[dict]:
    """Regenerate per-deploy fields on each cron-job template.

    The slice carries cron jobs with ``id``, ``sessionKey``, ``state``,
    ``createdAtMs``, and ``updatedAtMs`` already stripped (by
    ``catalog_slice.filter_cron_jobs_for_agent``). For each remaining
    template entry, we set:
      - a fresh ``id`` (random UUID)
      - the deployer's new ``agentId``
      - ``delivery.accountId`` rewritten to the new agent id — OpenClaw's
        per-agent channel routing convention pairs ``accountId`` with
        ``agentId`` (see ``_ensure_channel_bindings_for_patch`` in
        ``config_patcher`` for the same 1:1 mapping). Carrying the
        publisher's accountId would silently route delivery to the wrong
        per-agent channel on the deployer's container.
      - a freshly-derived ``sessionKey`` matching OpenClaw's
        ``agent:{agentId}:{userId}`` shape (the persisted format observed
        in ``cron/jobs.json``)
      - ``createdAtMs`` and ``updatedAtMs`` set to now
      - ``state`` is intentionally absent — OpenClaw recomputes
        ``nextRunAtMs`` from ``schedule`` on first read.

    ``delivery.channel`` and ``delivery.mode`` are carried as-is. If the
    deployer hasn't bound the named channel yet, the cron run will fail
    at delivery time and OpenClaw's ``failureAlert`` flow surfaces the
    error.
    """
    if not template_jobs:
        return []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    owner_id_lower = owner_id.lower()
    out: list[dict] = []
    for job in template_jobs:
        if not isinstance(job, dict):
            continue
        new_job = copy.deepcopy(job)
        new_job["id"] = str(uuid.uuid4())
        new_job["agentId"] = new_agent_id
        new_job["sessionKey"] = f"agent:{new_agent_id}:{owner_id_lower}"
        new_job["createdAtMs"] = now_ms
        new_job["updatedAtMs"] = now_ms
        # Codex P1: per-agent channel bindings pair ``accountId`` 1:1 with
        # ``agentId`` (see config_patcher._ensure_channel_bindings_for_patch).
        # Rewrite delivery.accountId to the new agent id so the cron lands on
        # the deployer's binding instead of the publisher's.
        delivery = new_job.get("delivery")
        if isinstance(delivery, dict) and "accountId" in delivery:
            delivery["accountId"] = new_agent_id
        # Drop any stale ``state`` carried through (defensive — slice should
        # already have stripped it, but jobs.json from older publishes may
        # have leaked it through).
        new_job.pop("state", None)
        out.append(new_job)
    return out


class CatalogService:
    def __init__(
        self,
        *,
        workspace,
        apply_deploy_aux_mutations: Callable[..., Awaitable[None]],
        pair_fn: Callable[..., Awaitable[None]],
        gateway_pool_factory: Callable[[], Any] | None = None,
    ):
        self._workspace = workspace
        self._apply_aux = apply_deploy_aux_mutations
        self._pair_fn = pair_fn
        self._gateway_pool_factory = gateway_pool_factory

    def _get_gateway_pool(self):
        if self._gateway_pool_factory is not None:
            return self._gateway_pool_factory()
        from core.containers import get_gateway_pool

        return get_gateway_pool()

    async def deploy_from_artifact(
        self,
        *,
        owner_id: str,
        slug: str,
        manifest: dict,
        slice_: dict,
        tar_bytes: bytes,
        container_ip: str,
        container_token: str,
        new_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Deploy a pre-fetched artifact (tarball + slice + manifest) into the
        owner's container.

        Steps:
          1. Extract ``tar_bytes`` into EFS at ``workspaces/{new_agent_id}/``.
          2. If the slice carries an agent, register it LIVE via
             ``pair_with_openclaw`` over the gateway — bypassing the
             chokidar reload lag that plagued the file-merge path. The
             slice's agent dict (minus ``id`` / ``workspace``) rides
             along on the ``agents.update`` RPC as ``extra_update_fields``.
          3. Deep-merge plugins/mcp/skills into the owner's openclaw.json
             via ``apply_deploy_aux_mutations`` (no agent_entry write —
             the agent is already in memory from step 2).
          4. Write the ``.template`` sidecar.
          5. Append cron jobs.

        Args:
            container_ip / container_token: resolved by the caller via
                ``ecs_manager.resolve_running_container(owner_id)``. No
                defaults — a deploy without a reachable container is
                not a supported mode.
            new_agent_id: pinned buyer-side OpenClaw agent_id. Marketplace
                deploys pass Paperclip's freshly-minted UUID so the two
                systems share the same id. Defaults to a locally-generated
                ``agent_<hex>`` UUID for direct callers.

        Rollback posture (v0): no rollback. A failed step lets the
        exception propagate. Partial state on EFS is functionally
        invisible — orphans without a sidecar don't show in the
        installed-templates UI, and they share no openclaw.json or
        cron/jobs.json identity with anything else. A successful pair
        followed by a later failure leaves a live in-memory agent that
        the buyer can chat with; wiping its workspace files would
        phantom it. Same posture as channel-binding deep-merges and
        plugin entries elsewhere in the codebase — none are reversible.
        """
        if new_agent_id is None:
            new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"

        self._workspace.extract_tarball_to_workspace(
            user_id=owner_id,
            agent_id=new_agent_id,
            tar_bytes=tar_bytes,
        )

        slice_ = slice_ or {}
        slice_has_agent = bool(slice_.get("agent"))
        agent_entry: dict[str, Any] = {}

        if slice_has_agent:
            # Build the agent entry locally so we know the new id + workspace
            # path. Deep-copy the slice's agent dict so we don't mutate the
            # caller's state (matters if s3 returns a cached/shared dict).
            agent_entry = copy.deepcopy(slice_.get("agent") or {})
            agent_entry["id"] = new_agent_id
            agent_entry["workspace"] = f".openclaw/workspaces/{new_agent_id}"
            # Strip the helper-owned fields from the extras dict — ``id``
            # and ``workspace`` are derived from the create RPC's
            # ``name`` (the agent UUID) so they must not be forwarded as
            # update fields. pair_with_openclaw also force-overrides
            # ``agentId`` + ``name`` regardless of caller input, but
            # being explicit here keeps the contract obvious.
            extra_update_fields = {k: v for k, v in agent_entry.items() if k not in ("id", "workspace")}

            # Synthesize the Paperclip-shaped dict the helper expects.
            # The actual Paperclip agent was minted upstream in the
            # marketplace router; we're reusing its UUID + name to drive
            # the OpenClaw gateway RPC. No Paperclip API call here.
            paperclip_agent: dict[str, Any] = {
                "id": new_agent_id,
                "name": agent_entry.get("name") or new_agent_id,
            }
            if agent_entry.get("model"):
                paperclip_agent["model"] = agent_entry["model"]

            await self._pair_fn(
                owner_id=owner_id,
                paperclip_agent=paperclip_agent,
                gateway_pool=self._get_gateway_pool(),
                ip=container_ip,
                token=container_token,
                extra_update_fields=extra_update_fields,
            )

            # Selection-aware additions (marketplace publish flow): the
            # seller picked specific MCP servers + skills in the publish
            # wizard; those land in the slice under ``mcp`` / ``skills``
            # with secret fields already scrubbed to "" by
            # ``catalog_slice``. The buyer fills in credentials before
            # the integration becomes functional. ``None`` for either
            # patch keeps legacy listing behavior (no MCP / skills
            # registration) — backwards compatible with pre-bundle
            # listings already in the marketplace.
            plugins_patch = copy.deepcopy(slice_.get("plugins") or {})
            mcp_patch = copy.deepcopy(slice_.get("mcp")) or None
            # Substitute __AGENT_DIR__ → new_agent_id in any mcp args
            # before applying. Pairs with agent_export's publish-time
            # rewrite that swaps the seller's agent_id out for the
            # sentinel; here we put the buyer's id in.
            mcp_patch = _substitute_agent_dir_sentinel(mcp_patch, new_agent_id)
            skills_patch = copy.deepcopy(slice_.get("skills")) or None

            await self._apply_aux(
                owner_id,
                plugins_patch=plugins_patch,
                mcp_patch=mcp_patch,
                skills_patch=skills_patch,
            )

        # ``manifest["version"]`` is sourced from the listing row's
        # ``manifest_json`` column (boto3 returns DDB Numbers as
        # ``Decimal``), or from ``listing["version"]`` setdefaulted in
        # the marketplace router. Either way it can be ``Decimal``,
        # which the sidecar's ``json.dumps`` refuses to serialize and
        # blows up the deploy with TypeError. Coerce to int at the
        # write boundary — the field is always a small positive
        # integer in practice (listing version starts at 1).
        raw_version = manifest.get("version", 1)
        try:
            template_version = int(raw_version) if raw_version is not None else 1
        except (TypeError, ValueError):
            template_version = 1
        self._workspace.write_template_sidecar(
            user_id=owner_id,
            agent_id=new_agent_id,
            content={
                "template_slug": slug,
                "template_version": template_version,
                "deployed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Carry the publisher's cron jobs (filtered + stripped at publish
        # time) over to the deployer's cron/jobs.json with regenerated
        # ids and the new agent_id wired in. Done AFTER the agent is
        # registered so the runtime never sees a cron job pointing at a
        # nonexistent agent. Skipped when there's no slice agent — a
        # cron job pointing at an unregistered agent would just error at
        # delivery time.
        template_cron_jobs = slice_.get("cron_jobs") or []
        if template_cron_jobs and slice_has_agent:
            from core.services.config_patcher import append_cron_jobs

            await append_cron_jobs(
                owner_id,
                _remap_cron_jobs_for_deploy(
                    template_cron_jobs,
                    new_agent_id=new_agent_id,
                    owner_id=owner_id,
                ),
            )

        return {
            "slug": slug,
            "version": manifest.get("version", 1),
            "agent_id": new_agent_id,
            "name": manifest.get("name", slug),
            "skills_added": list(agent_entry.get("skills") or []),
            # OpenClaw plugin entries live at ``plugins.entries.{name}``;
            # the top-level keys (``slots``/``entries``) are structural
            # containers, not plugin names.
            "plugins_enabled": list(((slice_.get("plugins") or {}).get("entries") or {}).keys()),
            "cron_jobs_added": (len(slice_.get("cron_jobs") or []) if slice_has_agent else 0),
            "config_registered": slice_has_agent,
        }

    # ---- deployed ----

    def list_deployed_for_user(self, owner_id: str) -> list[dict[str, Any]]:
        """Scan the owner's workspaces for .template sidecars; return provenance.

        Scans ``workspaces/`` (where sidecars live and where deploy writes
        immediately) rather than ``agents/`` (OpenClaw runtime state that
        lags behind openclaw.json updates). Caller must pass the same
        owner_id used during ``deploy()`` — the router resolves it via
        ``core.auth.resolve_owner_id(auth)`` so deploy + list stay keyed
        consistently for both personal and org-context callers.
        """
        deployed = []
        for agent_id in self._workspace.list_workspace_agent_dirs(owner_id):
            sidecar = self._workspace.read_template_sidecar(owner_id, agent_id)
            if sidecar:
                deployed.append(
                    {
                        "agent_id": agent_id,
                        "template_slug": sidecar.get("template_slug"),
                        "template_version": sidecar.get("template_version"),
                    }
                )
        return deployed


_catalog_service: CatalogService | None = None


def get_catalog_service() -> CatalogService:
    """Returns the singleton CatalogService used by marketplace deploys.

    No longer reads from the admin-catalog S3 bucket — publish + unpublish
    moved to the marketplace flow (#547). The remaining surface is
    ``deploy_from_artifact`` (called by /marketplace/listings/{slug}/deploy
    after fetching the tarball + slice from the marketplace artifacts
    bucket) and ``list_deployed_for_user`` (called by
    /marketplace/me/installed-templates to drive the sidebar's
    already-deployed filter).
    """
    global _catalog_service
    if _catalog_service is not None:
        return _catalog_service

    from core.containers import get_workspace
    from core.services.config_patcher import apply_deploy_aux_mutations
    from core.services.paperclip_openclaw_pairing import pair_with_openclaw

    _catalog_service = CatalogService(
        workspace=get_workspace(),
        apply_deploy_aux_mutations=apply_deploy_aux_mutations,
        pair_fn=pair_with_openclaw,
    )
    return _catalog_service
