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

import asyncio
import copy
import io
import logging
import tarfile
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from core.services import agent_export
from core.services.catalog_slice import strip_removed_agent_entry_keys
from core.services.credentials.mcp_server import reconcile_integrations

logger = logging.getLogger(__name__)


# Mirrors agent_export._AGENT_DIR_SENTINEL. Duplicated rather than
# imported because catalog_service is on the deploy side and shouldn't
# need to depend on agent_export's symbol surface. If a third caller
# emerges, move to a shared constants module.
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"


class MissingCredentialsError(Exception):
    """Raised when a deploy targets providers the buyer hasn't connected.

    Plain typed exception (not HTTPException) so catalog_service stays
    HTTP-agnostic — the router maps this to a 409. Carries the sorted list
    of missing provider keys so the UI can route the buyer straight to the
    integrations panel for exactly those providers.
    """

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"missing required credentials: {', '.join(missing)}")


async def assert_credentials_satisfied(
    owner_id: str,
    manifest: dict,
    slice_: dict | None = None,
) -> None:
    """Block a deploy when the agent needs providers the buyer hasn't connected.

    The required set is the union of two sources:
      1. Explicit ``manifest.requires.credentials`` entries whose ``optional``
         flag is falsy — the seller's declared hard dependencies.
      2. Providers *implied* by the slice's bundled MCP servers. A registry
         MCP server's key equals its provider key (e.g. ``mcp.servers.stripe``
         → provider ``stripe``), and catalog_slice scrubs the secret values
         while keeping the keys, so an unconnected provider would otherwise
         deploy an MCP that 401s on first use. We only count slice MCP keys
         that are known registry providers — arbitrary/custom MCP servers
         aren't credential-gated here.

    A provider is "connected" when the owner has an integrations row for it
    whose status is ``connected`` (the default written by integrations_repo).
    Raises ``MissingCredentialsError`` (sorted, deduped) when any required
    provider is unconnected; returns ``None`` when all are satisfied.
    """
    from core.repositories import integrations_repo
    from core.services.credentials import registry

    known_providers = registry.all_providers()

    required: set[str] = set()
    # ``requires`` (and its ``credentials``) may be absent OR present-but-null —
    # ``"requires": null`` is a common JSON encoding of an empty optional block.
    # ``.get(key, default)`` only falls back on ABSENT keys, so use ``or`` to
    # also normalize null/non-dict to empty and avoid an AttributeError -> 500.
    requires = (manifest or {}).get("requires") or {}
    for entry in (requires.get("credentials") if isinstance(requires, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("optional"):
            continue
        provider = entry.get("provider")
        if provider:
            required.add(provider)

    # The slice's MCP servers live under ``mcp.servers`` (nested dict — same
    # path _substitute_agent_dir_sentinel reads). A server key that matches a
    # registry provider key implies that credential is needed.
    mcp_servers = ((slice_ or {}).get("mcp") or {}).get("servers") or {}
    if isinstance(mcp_servers, dict):
        required |= {key for key in mcp_servers if key in known_providers}

    if not required:
        return

    connected = {
        row["provider"]
        for row in await integrations_repo.list_for_owner(owner_id)
        if row.get("provider") and row.get("status", "connected") == "connected"
    }

    missing = sorted(required - connected)
    if missing:
        raise MissingCredentialsError(missing)


def warn_rows(manifest: dict) -> list[dict]:
    """Requirements the buyer should SEE at hire but that never block a deploy.

    ``byo`` (bring-your-own-key) and ``unavailable`` (gated / phantom) rows
    can't be verified or injected by the platform, so the hire gate surfaces
    them as honest warnings ("these steps will skip") rather than 409ing on
    something the buyer has no way to satisfy here.
    """
    return [
        r
        for r in (manifest or {}).get("requirements", []) or []
        if isinstance(r, dict) and r.get("disposition") in ("byo", "unavailable")
    ]


# Seller-authored files that become code or an autonomous run the moment
# they land on the buyer's EFS: OpenClaw discovers a workspace
# ``openclaw.plugin.json`` (or a package.json carrying an
# ``openclaw.extensions`` key) as a loadable in-process plugin, and a
# workspace ``BOOT.md`` triggers an LLM run on every gateway restart
# (internal hooks are enabled fleet-wide). The export side no longer ships
# these, but published artifacts are immutable — so the deploy strips them
# too. Stripped and logged, never hard-failed: old listings may carry them.
_BLOCKED_TAR_BASENAMES = frozenset({"BOOT.md", "openclaw.plugin.json"})


def _strip_plugin_boot_vectors(tar_bytes: bytes, *, slug: str) -> bytes:
    """Drop workspace-plugin / boot-run members from an artifact tarball.

    Basename match at any depth. Bytes that don't parse as a tar.gz are
    returned unchanged — ``untar_to_directory`` is the parser of record and
    surfaces the real error on the extract path. When nothing is stripped the
    ORIGINAL bytes are returned, preserving the artifact's deterministic byte
    identity.
    """
    try:
        src = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        return tar_bytes

    stripped: list[str] = []
    out_buf = io.BytesIO()
    with src, tarfile.open(fileobj=out_buf, mode="w:gz") as out:
        for member in src.getmembers():
            basename = member.name.rsplit("/", 1)[-1]
            if basename in _BLOCKED_TAR_BASENAMES:
                stripped.append(member.name)
                continue
            data = src.extractfile(member) if member.isfile() else None
            if basename == "package.json" and data is not None:
                raw = data.read()
                if agent_export.package_json_declares_openclaw_extensions(raw):
                    stripped.append(member.name)
                    continue
                out.addfile(member, io.BytesIO(raw))
                continue
            if data is not None:
                out.addfile(member, data)
            else:
                out.addfile(member)

    if not stripped:
        return tar_bytes
    for name in stripped:
        logger.warning(
            "deploy: stripped untrusted artifact member %r for listing %s",
            name,
            slug,
        )
    return out_buf.getvalue()


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
                # Artifacts published before the workspace-layout migration
                # carry ``workspaces/<sentinel>`` paths; normalize to the
                # upstream-convention ``workspace-<sentinel>`` form BEFORE
                # substituting the buyer's agent id.
                a.replace(f"workspaces/{_AGENT_DIR_SENTINEL}", f"workspace-{_AGENT_DIR_SENTINEL}").replace(
                    _AGENT_DIR_SENTINEL, new_agent_id
                )
                if isinstance(a, str)
                else a
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
        # Per-agent channel bindings pair ``accountId`` 1:1 with ``agentId``
        # (see config_patcher._ensure_channel_bindings_for_patch).
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
          1. Extract ``tar_bytes`` into EFS at ``workspace-{new_agent_id}/``.
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

        Rollback posture: a failed step lets the exception propagate to
        the caller, which is the marketplace deploy router — the owner of
        the deploy transaction (it also mints + rolls back the Paperclip
        agent). On failure it calls ``unwind_partial_deploy`` to remove
        the openclaw.json ``agents.list`` entry and the on-EFS agent dirs
        this method created, so a failed Hire leaves no orphan and the
        buyer can cleanly retry. ``new_agent_id`` is the shared handle
        that makes the unwind precise.
        """
        if new_agent_id is None:
            new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"

        # Credential gate — runs before any EFS / gateway side effect so a
        # blocked deploy leaves no partial state. The buyer must have
        # connected every provider the agent hard-depends on (declared in the
        # manifest or implied by the bundled MCP servers); otherwise the agent
        # would deploy and immediately fail on its first credentialed call.
        await assert_credentials_satisfied(owner_id, manifest, slice_)

        self._workspace.extract_tarball_to_workspace(
            user_id=owner_id,
            agent_id=new_agent_id,
            tar_bytes=_strip_plugin_boot_vectors(tar_bytes, slug=slug),
        )

        slice_ = slice_ or {}
        slice_has_agent = bool(slice_.get("agent"))
        agent_entry: dict[str, Any] = {}

        if slice_has_agent:
            # Build the agent entry locally so we know the new id + workspace
            # path. Deep-copy the slice's agent dict so we don't mutate the
            # caller's state (matters if s3 returns a cached/shared dict).
            agent_entry = strip_removed_agent_entry_keys(slice_.get("agent") or {})
            agent_entry["id"] = new_agent_id
            agent_entry["workspace"] = f".openclaw/workspace-{new_agent_id}"

            # Per-agent containment: marketplace agents carry third-party-
            # authored instructions. Without allowAgents pinned to the agent's
            # own id, a deployed agent can sessions_spawn under ANY sibling id
            # — including the premium-pinned CEO — and the ``gateway`` tool
            # allows limited config self-modification. Deny, never a narrowing
            # allow: an explicit allow-list that resolves to zero callable
            # tools kills runs. These can't ride the pair RPC (agents.update's
            # strict schema only accepts agentId/name/workspace/model/emoji/
            # avatar), so they land via the on-disk agents.list merge below.
            # Seller-authored values are clamped, not trusted: allowAgents is
            # overwritten, deny entries are unioned with ours.
            subagents = agent_entry.get("subagents")
            if not isinstance(subagents, dict):
                subagents = {}
            agent_entry["subagents"] = {**subagents, "allowAgents": [new_agent_id]}
            tools = agent_entry.get("tools")
            if not isinstance(tools, dict):
                tools = {}
            deny = [t for t in tools.get("deny") or [] if isinstance(t, str)]
            deny.extend(t for t in ("gateway", "sessions_send") if t not in deny)
            agent_entry["tools"] = {**tools, "deny": deny}

            # OpenClaw's ``agents.update`` AJV schema is strict
            # (additionalProperties: false) and only accepts
            # ``agentId, name, workspace, model, emoji, avatar``.
            # Forwarding anything else (the seller's ``identity`` block as
            # a nested dict, ``skills``, ``reasoningDefault``, etc.) makes
            # the whole RPC 422 — leaving the freshly-created agent stuck
            # with ``name=<uuid>`` from ``agents.create`` and showing the
            # UUID in the buyer's sidebar.
            # See openclaw/src/gateway/protocol/schema/agents-models-skills.ts:97.
            # The pairing helper sets ``agentId`` + ``name`` itself, so we
            # only need to thread the optional identity decorations here.
            identity = agent_entry.get("identity")
            extra_update_fields: dict[str, Any] = {}
            if isinstance(identity, dict):
                if identity.get("emoji"):
                    extra_update_fields["emoji"] = identity["emoji"]
                if identity.get("avatar"):
                    extra_update_fields["avatar"] = identity["avatar"]

            # Synthesize the Paperclip-shaped dict the helper expects.
            # The actual Paperclip agent was minted upstream in the
            # marketplace router; we're reusing its UUID + name to drive
            # the OpenClaw gateway RPC. No Paperclip API call here.
            # Prefer the top-level ``name`` field but fall back to the
            # ``identity.name`` (OpenClaw persists both, but seller agents
            # exported from older snapshots may only have the nested one).
            display_name = (
                agent_entry.get("name")
                or (identity.get("name") if isinstance(identity, dict) else None)
                or new_agent_id
            )
            paperclip_agent: dict[str, Any] = {
                "id": new_agent_id,
                "name": display_name,
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

            # Also file-merge the full agent_entry into agents.list so the
            # per-agent fields OpenClaw's narrow agents.update RPC schema
            # can't accept (``skills`` binding, ``reasoningDefault``,
            # ``tts``, etc.) land in the buyer's openclaw.json. Keys
            # upstream dropped from the strict AgentEntrySchema are stripped
            # at the deepcopy above so an older immutable artifact can't
            # write a now-invalid key. Chokidar (CHOKIDAR_USEPOLLING
            # on EFS, 1-30s) reloads and the in-memory agent picks them
            # up. Display-level fields (``name``, ``identity``) were
            # already set instantly via the RPC pair above, so the
            # sidebar doesn't flicker through the lag.
            await self._apply_aux(
                owner_id,
                agent_entry=agent_entry,
                plugins_patch=plugins_patch,
                mcp_patch=mcp_patch,
                skills_patch=skills_patch,
            )

            # The deep-merge above lands the slice's registry-provider servers
            # with scrubbed-blank credentials. Re-assert the buyer's own
            # connected integrations from the durable sources, overwriting those
            # keys with the canonical server + the buyer's token. Best-effort: a
            # failure leaves a deployed-but-uncredentialed slot the buyer can
            # re-connect, never aborts an already-deployed agent.
            try:
                await reconcile_integrations(owner_id)
            except Exception:
                logger.exception(
                    "integration reconcile failed during deploy for %s; agent deployed without integration servers",
                    owner_id,
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

        # ACP coding-harness runtime. If the seller declared the agent
        # delegates to harnesses (manifest.requires_acp), turn on the buyer
        # container's container-wide ACP capability. Best-effort: a failure
        # here must not strand an already-deployed agent — the buyer just
        # gets an agent that can't spawn a harness (same posture as the
        # integration reconcile above).
        acp_requested = bool(manifest.get("requires_acp"))
        acp_enabled = False
        try:
            acp_enabled = await self._maybe_enable_acp(owner_id, manifest)
        except Exception:
            logger.exception(
                "deploy: ACP enable failed for %s; agent deployed without harness delegation",
                owner_id,
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
            # Lets the storefront distinguish "deployed with harness
            # delegation live" from "needs ACP but it couldn't be turned on"
            # (flag off, or the buyer's provider can't auth any harness) so it
            # can render a soft note instead of silently shipping a degraded
            # agent.
            "acp_requested": acp_requested,
            "acp_enabled": acp_enabled,
        }

    async def unwind_partial_deploy(self, owner_id: str, new_agent_id: str) -> None:
        """Reverse the container/EFS side effects of a failed deploy.

        Called by the marketplace deploy router when any post-Paperclip step
        raises, so a failed Hire leaves no orphan agent in the buyer's
        container. Removes the ``agents.list`` entry the deploy added to
        openclaw.json (the container's ``agents.create`` may persist it even
        when its RPC response is lost) and the on-EFS agent dirs the tarball
        extract created.

        Both operations are idempotent and best-effort: a missing entry / dir
        is a no-op, and a failure here is logged but never raised — the
        caller must surface the ORIGINAL deploy error, not a cleanup error.
        Removing the openclaw.json entry lets the container's file watcher
        drop the now-orphaned agent from memory on its next reload.
        """
        from core.services.config_patcher import remove_from_openclaw_config_list

        try:
            await remove_from_openclaw_config_list(
                owner_id,
                ["agents", "list"],
                lambda entry: isinstance(entry, dict) and entry.get("id") == new_agent_id,
            )
        except Exception:
            logger.exception(
                "unwind_partial_deploy: failed to remove openclaw.json agent entry %s for %s",
                new_agent_id,
                owner_id,
            )

        try:
            # cleanup_agent_dirs is sync (shutil.rmtree) over EFS-backed dirs
            # that can be large enough to block the event loop and stall
            # unrelated WS chat traffic, so run it on a worker thread — same
            # posture as the agents.delete path in websocket_chat.py.
            await asyncio.to_thread(self._workspace.cleanup_agent_dirs, owner_id, new_agent_id)
        except Exception:
            logger.exception(
                "unwind_partial_deploy: failed to clean EFS dirs for agent %s owner %s",
                new_agent_id,
                owner_id,
            )

    async def _maybe_enable_acp(self, owner_id: str, manifest: dict) -> bool:
        """Turn on the buyer container's ACP coding-harness runtime when the
        deployed listing declares ``requires_acp``.

        Returns True iff the ACP block was written. Returns False (no-op) when:
          - the listing doesn't require ACP,
          - ``settings.ACP_ENABLED`` is off (the platform-wide rollout flag —
            the acpx runtime is only baked + promoted behind it),
          - the buyer's provider can't authenticate any harness
            (``derive_enabled_harnesses`` returns []; e.g. a Gemini-only owner).

        ACP is a container-wide capability — OpenClaw's gate is the root
        ``acp.enabled`` flag read by ``isAcpRuntimeSpawnAvailable``, plus a
        registered backend (the acpx plugin). There is NO per-agent ACP config
        key (the agent entry schema is ``.strict()``), so we patch the
        container's root ``acp`` block + the ``plugins.entries.acpx`` entry —
        reusing the exact builder the provision path uses so a deployed
        container matches a freshly-provisioned one. Idempotent: re-deploying
        another ACP agent just re-merges the same block.
        """
        if not manifest.get("requires_acp"):
            return False

        from core.config import settings

        if not settings.ACP_ENABLED:
            logger.info(
                "deploy: listing requires ACP but ACP_ENABLED is off; skipping for %s",
                owner_id,
            )
            return False

        from core.containers.config import build_acp_config, derive_enabled_harnesses
        from core.services.config_patcher import patch_openclaw_config
        from core.services.container_provision import resolve_provider_choice

        provider_choice, byo_provider = await resolve_provider_choice(owner_id)
        harnesses = derive_enabled_harnesses(provider_choice, byo_provider)
        if not harnesses:
            logger.info(
                "deploy: ACP requested but provider=%s (byo=%s) can't auth any harness; skipping for %s",
                provider_choice,
                byo_provider,
                owner_id,
            )
            return False

        acp_block, acpx_plugin_entry = build_acp_config(harnesses)
        await patch_openclaw_config(
            owner_id,
            {"acp": acp_block, "plugins": {"entries": acpx_plugin_entry}},
        )
        logger.info(
            "deploy: enabled ACP for %s (harnesses=%s)",
            owner_id,
            harnesses,
        )
        return True

    # ---- deployed ----

    def list_deployed_for_user(self, owner_id: str) -> list[dict[str, Any]]:
        """Scan the owner's workspaces for .template sidecars; return provenance.

        Scans the per-agent ``workspace-{id}`` dirs (where sidecars live and
        where deploy writes immediately) rather than ``agents/`` (OpenClaw
        runtime state that lags behind openclaw.json updates). Caller must pass the same
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
