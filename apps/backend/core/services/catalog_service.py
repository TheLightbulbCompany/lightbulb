"""Catalog service — deploy + already-deployed listing.

Publish + unpublish moved to the marketplace flow (see
``routers.marketplace_listings`` + ``core.services.marketplace_service``).
What's left here:
  - ``deploy_from_artifact``: extracts a tarball into the buyer's EFS
    workspace and deep-merges the openclaw-slice into their openclaw.json.
    Called by /marketplace/listings/{slug}/deploy.
  - ``list_deployed_for_user``: scans the buyer's EFS for ``.template``
    sidecars to drive the chat sidebar's already-deployed filter. Called
    by /marketplace/me/installed-templates.

Depends on injected collaborators so unit tests can mock them:
  - workspace: Workspace (from core.containers.workspace)
  - apply_deploy_mutation: async callable (from core.services.config_patcher)
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
            entry["args"] = [
                a.replace(_AGENT_DIR_SENTINEL, new_agent_id) if isinstance(a, str) else a
                for a in args
            ]
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
        apply_deploy_mutation: Callable[[str, dict, dict], Awaitable[None]],
    ):
        self._workspace = workspace
        self._apply_deploy = apply_deploy_mutation

    async def deploy_from_artifact(
        self,
        *,
        owner_id: str,
        slug: str,
        manifest: dict,
        slice_: dict,
        tar_bytes: bytes,
        new_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Deploy a pre-fetched artifact (tarball + slice + manifest) into the
        owner's container — extracts files into EFS at
        ``workspaces/{new_agent_id}/`` and (when slice_.agent is present)
        deep-merges the slice into the owner's openclaw.json so the agent
        auto-registers.

        If ``slice_`` is falsy / lacks an ``agent`` block, we extract the
        workspace files but skip the openclaw.json mutation. Used by legacy
        marketplace listings published before slice-synthesis shipped — the
        owner's config doesn't auto-register and they have to add the
        agent entry by hand.

        Args:
            new_agent_id: When supplied, the buyer-side OpenClaw agent_id is
                pinned to this value. Marketplace deploys pass Paperclip's
                freshly-minted UUID so the two systems share the same id.
                Defaults to a locally-generated ``agent_<hex>`` UUID for
                backwards-compatible direct callers.
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

        try:
            if slice_has_agent:
                # Build agent entry with new id + workspace path.
                # Deep-copy the slice's agent dict so we don't mutate the caller's state
                # (matters if s3 returns a cached/shared dict).
                agent_entry = copy.deepcopy(slice_.get("agent") or {})
                agent_entry["id"] = new_agent_id
                agent_entry["workspace"] = f".openclaw/workspaces/{new_agent_id}"

                # Apply the mutation atomically inside the config file lock so two
                # concurrent deploys cannot drop each other's agent entries.
                plugins_patch = copy.deepcopy(slice_.get("plugins") or {})

                # Selection-aware additions (marketplace publish flow): the
                # seller picked specific MCP servers + skills in the publish
                # wizard; those land in the slice under ``mcp`` / ``skills``
                # with secret fields already scrubbed to "" by
                # ``catalog_slice``. The buyer fills in credentials before
                # the integration becomes functional. ``None`` for either
                # patch keeps legacy listing behavior (no MCP / skills
                # registration) — backwards compatible with pre-bundle
                # listings already in the marketplace.
                mcp_patch = copy.deepcopy(slice_.get("mcp")) or None
                # Substitute __AGENT_DIR__ → new_agent_id in any mcp args
                # before applying. Pairs with agent_export's publish-time
                # rewrite that swaps the seller's agent_id out for the
                # sentinel; here we put the buyer's id in.
                mcp_patch = _substitute_agent_dir_sentinel(mcp_patch, new_agent_id)
                skills_patch = copy.deepcopy(slice_.get("skills")) or None

                await self._apply_deploy(
                    owner_id,
                    agent_entry,
                    plugins_patch,
                    mcp_patch=mcp_patch,
                    skills_patch=skills_patch,
                )

            self._workspace.write_template_sidecar(
                user_id=owner_id,
                agent_id=new_agent_id,
                content={
                    "template_slug": slug,
                    "template_version": manifest.get("version", 1),
                    "deployed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            # Carry the publisher's cron jobs (filtered + stripped at publish
            # time) over to the deployer's cron/jobs.json with regenerated
            # ids and the new agent_id wired in. Done AFTER the agent entry
            # is in place so the runtime never sees a cron job pointing at a
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
        except Exception:
            # Roll back the extracted workspace so a failed deploy doesn't
            # leave orphan files on EFS. cleanup_agent_dirs removes both
            # agents/{id}/ and workspaces/{id}/, and is best-effort +
            # idempotent — safe to call even if the config patch never
            # ran or already succeeded.
            self._workspace.cleanup_agent_dirs(owner_id, new_agent_id)
            # Codex P2: ``apply_deploy_mutation`` may have already committed
            # the agent entry to openclaw.json before a later step (sidecar
            # write, cron append) failed. Without an inverse mutation, retries
            # would accumulate dangling agent entries that openclaw_id never
            # gets workspace files for. Best-effort remove from agents.list;
            # swallow secondary failures so the original exception surfaces.
            # Only attempt the rollback if we actually called _apply_deploy.
            if slice_has_agent:
                try:
                    from core.services.config_patcher import (
                        remove_from_openclaw_config_list,
                    )

                    await remove_from_openclaw_config_list(
                        owner_id,
                        ["agents", "list"],
                        lambda a: isinstance(a, dict) and a.get("id") == new_agent_id,
                    )
                except Exception:
                    pass
            # Known limitation (Codex P2): the plugins deep-merge from
            # apply_deploy_mutation isn't rolled back. Plugin code is
            # bundled in the OpenClaw image, so leaving extra plugins
            # enabled in config has no install side-effect — the merge
            # is also idempotent on retry. Inverting a deep-merge
            # cleanly requires a pre-mutation snapshot, which adds
            # complexity that isn't matched by the equivalent
            # ``patch_openclaw_config`` path (channel binding deep-merges
            # are likewise not reversible). Accepted as the same risk
            # posture as the rest of the codebase.
            raise

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
    moved to the marketplace flow (#547 + this PR). The remaining surface
    is ``deploy_from_artifact`` (called by /marketplace/listings/{slug}/deploy
    after fetching the tarball + slice from the marketplace artifacts
    bucket) and ``list_deployed_for_user`` (called by
    /marketplace/me/installed-templates to drive the sidebar's
    already-deployed filter).
    """
    global _catalog_service
    if _catalog_service is not None:
        return _catalog_service

    from core.containers import get_workspace
    from core.services.config_patcher import apply_deploy_mutation

    _catalog_service = CatalogService(
        workspace=get_workspace(),
        apply_deploy_mutation=apply_deploy_mutation,
    )
    return _catalog_service
