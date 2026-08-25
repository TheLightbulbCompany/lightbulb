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

from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.services import agent_export
from core.services.agent_roster import roster_entries
from core.services.catalog_slice import sanitize_agent_entry_for_schema
from core.services.credentials.mcp_server import reconcile_integrations
from core.services.paperclip_credential_rule import (
    PAPERCLIP_CREDENTIAL_PROHIBITION,
    PAPERCLIP_CREDENTIAL_RULE,
    PAPERCLIP_CREDS_PLUGIN_PATCH,
)
from core.dynamodb import utc_now_iso

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


def emit_agent_deployed(*, owner_id: str, slug: str | None, agent_id: str, seller_id: str | None = None) -> None:
    """Record the deploy funnel event — ONE emitter for all three deploy
    surfaces (marketplace deploy, approval hire, agent builder) so the
    properties can't drift apart.

    Deliberately NOT called from ``deploy_from_artifact``: the install isn't
    committed when that returns. Every caller still runs rollback-wrapped
    steps after it (operator-device pairing, routine recreate), and a failure
    there unwinds the container install and deletes the Paperclip agent.
    Emitting from inside would count those rolled-back hires as deployments.
    Each caller emits once its own commit steps have survived.
    """
    capture_product_event(
        distinct_id=owner_id,
        event="catalog_agent_deployed",
        properties={"slug": slug, "agent_id": agent_id, "seller_id": seller_id},
    )


# Seller-authored files that become code or an autonomous run the moment
# they land on the buyer's EFS: OpenClaw discovers a workspace
# ``openclaw.plugin.json`` (or a package.json carrying an
# ``openclaw.extensions`` key) as a loadable in-process plugin, and a
# workspace ``BOOT.md`` triggers an LLM run on every gateway restart
# (internal hooks are enabled fleet-wide). The export side no longer ships
# these, but published artifacts are immutable — so the deploy strips them
# too. Stripped and logged, never hard-failed: old listings may carry them.
_BLOCKED_TAR_BASENAMES = frozenset({"BOOT.md", "openclaw.plugin.json"})


def _deep_merge_plugins(base: dict, extra: dict) -> dict:
    """Merge ``extra`` into ``base`` one level into ``entries``.

    Deliberately narrow: the seller's plugin slice owns everything else, and
    all we ever add is one ``entries`` key. A blanket deep-merge here would
    risk reshaping seller-authored plugin config we do not own.
    """
    merged = dict(base)
    entries = dict(merged.get("entries") or {})
    entries.update(extra.get("entries") or {})
    merged["entries"] = entries
    return merged


def _append_credential_rule(workspace, owner_id: str, agent_id: str) -> None:
    """Append the Paperclip credential rule to a hired agent's AGENTS.md.

    Best-effort and idempotent: a hire must not fail because a seller shipped
    no playbook, or because the rule is already present (a listing published
    after the rule shipped carries it in the archive). Never raises — the
    prohibition is a safety net, not a precondition for the deploy.
    """
    rel = f"workspace-{agent_id}/AGENTS.md"
    try:
        try:
            body = workspace.read_file(owner_id, rel)
        except Exception:  # noqa: BLE001 — seller shipped no playbook
            body = ""
        if PAPERCLIP_CREDENTIAL_PROHIBITION in body:
            return
        workspace.write_file(owner_id, rel, body.rstrip("\n") + "\n" + PAPERCLIP_CREDENTIAL_RULE)
    except Exception:  # noqa: BLE001 — never fail a hire on the safety net
        logger.exception("catalog: credential-rule append failed owner=%s agent=%s", owner_id, agent_id)


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
      - a stable per-agent/index ``id`` (UUIDv5), so a retry after the cron
        write but before the deploy completion marker upserts the same job
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
      - ``enabled`` forced to False — deliberate product posture, matching
        routines: a seller's enabled hourly job would otherwise burn the
        buyer's inference budget from the moment of hire. The buyer flips
        it on when ready.
      - ``sessionTarget`` dropped — it names a seller-side session that
        doesn't exist on the buyer's container.
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
    for index, job in enumerate(template_jobs):
        if not isinstance(job, dict):
            continue
        new_job = copy.deepcopy(job)
        new_job["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"isol8:cron:v1:{new_agent_id}:{index}"))
        new_job["agentId"] = new_agent_id
        new_job["sessionKey"] = f"agent:{new_agent_id}:{owner_id_lower}"
        new_job["createdAtMs"] = now_ms
        new_job["updatedAtMs"] = now_ms
        new_job["enabled"] = False
        new_job.pop("sessionTarget", None)
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
        listing_id: str | None = None,
        seller_id: str | None = None,
    ) -> dict[str, Any]:
        """Deploy a pre-fetched artifact (tarball + slice + manifest) into the
        owner's container.

        Steps:
          1. Extract ``tar_bytes`` into EFS at ``workspace-{new_agent_id}/``.
          2. Write the ``.template`` sidecar — with the extract, NOT at the
             end, so every partial install is discoverable by the retry (see
             the comment at the write itself).
          3. If the slice carries an agent, register it LIVE via
             ``pair_with_openclaw`` over the gateway — bypassing the
             chokidar reload lag that plagued the file-merge path. The
             slice's agent dict (minus ``id`` / ``workspace``) rides
             along on the ``agents.update`` RPC as ``extra_update_fields``.
          4. Deep-merge plugins/mcp/skills into the owner's openclaw.json
             via ``apply_deploy_aux_mutations`` (no agent_entry write —
             the agent is already in memory from step 3).
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
            listing_id / seller_id: canonical marketplace identity recorded on
                the ``.template`` sidecar. Both are supplied by the trusted
                listing row, never the seller artifact. Direct callers such as
                agent builder omit them and therefore remain Custom.

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

        slice_ = slice_ or {}
        slice_has_agent = bool(slice_.get("agent"))
        # Remapped up front (ids are fresh UUIDs) so the sidecar below can
        # record them — the unwind needs the ids to pull a failed deploy's
        # jobs back out of cron/jobs.json. The append itself stays late,
        # after the agent is registered.
        remapped_cron_jobs = (
            _remap_cron_jobs_for_deploy(
                slice_.get("cron_jobs") or [],
                new_agent_id=new_agent_id,
                owner_id=owner_id,
            )
            if slice_has_agent
            else []
        )

        def _extract():
            # Tarball parse + EFS write are blocking; keep them off the loop.
            self._workspace.extract_tarball_to_workspace(
                user_id=owner_id,
                agent_id=new_agent_id,
                tar_bytes=_strip_plugin_boot_vectors(tar_bytes, slug=slug),
            )
            # The seller's AGENTS.md lands verbatim and is frozen in an
            # immutable artifact, so a listing published before the credential
            # rule existed — or any seller who simply never wrote one — leaves
            # this agent with no prohibition while Paperclip's wake text still
            # tells it to read the claimed-key file. Append ours after the
            # extract, additively and idempotently, so a hire is covered like
            # every other seed path. Deliberately AFTER extraction: the tar is
            # the seller's content and is not ours to rewrite in place.
            _append_credential_rule(self._workspace, owner_id, new_agent_id)

        await asyncio.to_thread(_extract)

        # Sidecar goes down with the FIRST durable side effect, not at the end.
        # It is what ``_live_install_of`` keys on, and a deploy that dies after
        # the tarball extract used to leave EFS dirs (plus, on a pair timeout, a
        # container-side agents.list entry) with NO sidecar — invisible to the
        # retry, which then minted a second agent and stranded the first as a
        # permanent ghost. Writing it first makes every partial install
        # discoverable. Two things keep it from handing back a DEAD id:
        # ``_live_install_of`` cross-checks the sidecar against openclaw.json's
        # ``agents.list`` (so a sidecar whose agent never registered reads as
        # "not installed"), and a non-timeout failure runs
        # ``unwind_partial_deploy``, which unlinks the sidecar FIRST — before
        # the two correlated, best-effort removals that can both fail and leave
        # the ``agents.list`` entry standing. The Teams lifecycle endpoint also
        # removes the sidecar and persisted config before dropping Paperclip,
        # so the normal owner-facing delete converges on the same invariant.
        #
        # ``manifest["version"]`` is sourced from the listing row's
        # ``manifest_json`` column (boto3 returns DDB Numbers as ``Decimal``), or
        # from ``listing["version"]`` setdefaulted in the marketplace router.
        # Either way it can be ``Decimal``, which the sidecar's ``json.dumps``
        # refuses to serialize and blows up the deploy with TypeError. Coerce to
        # int at the write boundary — the field is always a small positive
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
                "deployed_at": utc_now_iso(),
                "install_state": "installing",
                **({"listing_id": listing_id} if listing_id else {}),
                # Slug + seller is the install identity (slugs are not unique
                # across sellers); ids let the unwind remove this deploy's
                # appended cron jobs.
                **({"seller_id": seller_id} if seller_id else {}),
                "cron_job_ids": [j["id"] for j in remapped_cron_jobs],
            },
        )

        agent_entry: dict[str, Any] = {}
        dropped_keys: list[str] = []

        if slice_has_agent:
            # Build the agent entry locally so we know the new id + workspace
            # path. Deep-copy the slice's agent dict so we don't mutate the
            # caller's state (matters if s3 returns a cached/shared dict).
            # Whitelist-sanitize against the strict AgentEntrySchema: one
            # unrecognized key in the merged entry fails validation for the
            # WHOLE config and takes the owner's gateway down (Atlas's staging
            # config shipped an invented ``archetype``; frozen S3 artifacts can
            # carry retired keys). ``dropped_keys`` rides the return dict so
            # callers can surface the loss instead of it dying in the log.
            agent_entry, dropped_keys = sanitize_agent_entry_for_schema(slice_.get("agent") or {})
            if dropped_keys:
                put_metric("catalog_deploy.agent_entry_keys_dropped", value=len(dropped_keys))
                logger.warning(
                    "deploy_from_artifact: dropped non-schema agent-entry keys %s for owner=%s slug=%s",
                    dropped_keys,
                    owner_id,
                    slug,
                )
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
            # See openclaw/src/gateway/protocol/schema/agents-models-skills.ts.
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
            # exported from older snapshots may only have the nested one),
            # then the manifest name. The manifest fallback matters on the
            # agent-builder path: the slice strips ``id`` (which held the
            # display name) so without it the pair RPC and the file entry
            # would carry the raw UUID.
            display_name = (
                agent_entry.get("name")
                or (identity.get("name") if isinstance(identity, dict) else None)
                or manifest.get("name")
                or new_agent_id
            )
            # Make the on-disk entry carry the name too: the aux file-merge
            # below REPLACES the whole entry at this id, so an entry without
            # ``name`` clobbers the display name the pair RPC just set.
            if not agent_entry.get("name"):
                agent_entry["name"] = display_name
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
            # The rule appended at extract time claims PAPERCLIP_* is already
            # in the environment; this is what makes that true. A buyer
            # provisioned before the plugin shipped has it baked by the image
            # roll but not enabled (the roll reuses the on-disk config), so a
            # hire onto such a container would otherwise get a playbook whose
            # central claim is false. Merged into, not over, the seller's
            # plugin slice.
            plugins_patch = _deep_merge_plugins(plugins_patch, PAPERCLIP_CREDS_PLUGIN_PATCH)
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

        # Carry the publisher's cron jobs (filtered + stripped at publish
        # time) over to the deployer's cron/jobs.json with regenerated
        # ids and the new agent_id wired in. Done AFTER the agent is
        # registered so the runtime never sees a cron job pointing at a
        # nonexistent agent. Skipped when there's no slice agent — a
        # cron job pointing at an unregistered agent would just error at
        # delivery time.
        if remapped_cron_jobs:
            from core.services.config_patcher import append_cron_jobs

            await append_cron_jobs(owner_id, remapped_cron_jobs)

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

        # This is the commit record for retrying callers. The early sidecar and
        # agents.list entry are intentionally only a discoverable PARTIAL
        # footprint; pairing can persist them before aux config and cron jobs
        # land. A marker failure propagates so the caller unwinds the install.
        self._workspace.mark_template_install_complete(owner_id, new_agent_id)

        return {
            "slug": slug,
            "version": manifest.get("version", 1),
            "agent_id": new_agent_id,
            "name": manifest.get("name", slug),
            "origin": "Marketplace" if listing_id and seller_id else "Custom",
            **(
                {
                    "marketplace": {
                        "listingId": listing_id,
                        "slug": slug,
                        "version": template_version,
                        "seller": seller_id,
                    }
                }
                if listing_id and seller_id
                else {}
            ),
            "skills_added": list(agent_entry.get("skills") or []),
            # OpenClaw plugin entries live at ``plugins.entries.{name}``;
            # the top-level keys (``slots``/``entries``) are structural
            # containers, not plugin names.
            "plugins_enabled": list(((slice_.get("plugins") or {}).get("entries") or {}).keys()),
            "cron_jobs_added": (len(slice_.get("cron_jobs") or []) if slice_has_agent else 0),
            "config_registered": slice_has_agent,
            # Lets the storefront distinguish "deployed with harness
            # delegation live" from "needs ACP but it couldn't be turned on"
            # (the buyer's provider can't auth any harness) so it can render a
            # soft note instead of silently shipping a degraded agent.
            "acp_requested": acp_requested,
            "acp_enabled": acp_enabled,
            # Non-schema keys the sanitizer stripped from the slice's agent
            # entry — callers surface these to the operator (the agent-builder
            # bridge relays them to Atlas) instead of losing them to the log.
            "agent_entry_keys_dropped": dropped_keys,
        }

    async def unwind_partial_deploy(
        self, owner_id: str, new_agent_id: str, *, cause: BaseException | None = None
    ) -> bool:
        """Reverse the container/EFS side effects of a failed deploy.

        Returns True only when the unwind is SAFE — i.e. this agent can no
        longer read as installed. False means the container-side install may
        still be standing, so the Paperclip row must be kept too (see below).
        Two things return False, and callers must honour both:

        1. The unwind was SKIPPED because ``cause`` was a ``TimeoutError``.
        2. The ``.template`` sidecar could not be removed. The sidecar is half
           of what ``_live_install_of`` keys on, and the other two removals are
           correlated with it in failure (one EFS fault breaks all three) — so
           a surviving sidecar plus a surviving ``agents.list`` entry means
           every later Hire of that slug resolves to this agent id forever.
           Reporting True there and letting the caller delete the Paperclip row
           is what turns it into a ghost Teams cannot show OR delete; the whole
           point of unlinking the sidecar first is lost if its failure isn't
           reported. The other two removals are attempted regardless — they
           still shrink the litter — but they cannot make the unwind safe on
           their own.

        Called by every deploy-rollback site (marketplace deploy, the
        agent-builder bridge, company deploy) when a post-Paperclip step
        raises, so a failed Hire leaves no orphan agent in the buyer's
        container. Removes, in this order: the ``.template`` sidecar, the
        ``agents.list`` entry the deploy added to openclaw.json (the
        container's ``agents.create`` may persist it even when its RPC
        response is lost), and the on-EFS agent dirs the tarball extract
        created.

        **The sidecar goes FIRST, and that ordering is load-bearing.** The
        sidecar is written with the tarball extract now, and
        ``_live_install_of`` treats sidecar + a live ``agents.list`` entry as
        "already installed". The other two removals are independently
        best-effort and correlated in failure — one EFS stall breaks both, and
        ``cleanup_deleted_agent`` swallows its own error — so if the sidecar
        were removed last, an unwind whose first two steps failed would leave
        the sidecar AND the ``agents.list`` entry standing while the caller
        went on to delete the Paperclip row, and every later Hire of that slug
        would return ``already_installed`` pointing at an agent Teams cannot
        show, forever. A single-file unlink is the operation most likely to
        succeed regardless, and its absence is exactly what makes the retry
        safe.

        ``cause`` — the exception that triggered the rollback, if the caller
        has one. When it's a ``TimeoutError`` this unwind is skipped
        entirely (both the openclaw.json edit and the EFS rmtree). A
        client-side RPC timeout means "no answer yet", not "failed" — the
        container may still be mid-write on the very agent directory this
        would rm -rf. That's exactly what happened in production on
        2026-08-06: a rollback's unwind fired at 17:31:26 for a contended
        "Echo" deploy, and at 17:31:30 — 4.3s later — that agent's
        STILL-RUNNING ``agents.create`` failed with ``unsafe workspace file
        "IDENTITY.md"`` because the directory it was writing into had just
        been deleted out from under it. Two other creates in that same batch
        returned success afterward, after their state had already been torn
        down. Omitting ``cause`` (or passing a non-timeout exception) keeps the
        unwind unconditional, as before this carve-out existed.

        **The Paperclip row goes with it.** Skipping only this half is what
        created an unownable ghost: EFS dirs and (when ``agents.create`` landed
        but ``agents.update`` timed out) a container-side ``name=<uuid>`` agent
        survived, while the Paperclip row — the only handle the owner has to see
        or delete an agent from Teams — was deleted anyway. Nothing was left to
        clean it up, and the buyer's retry minted a duplicate beside it. So the
        two sides move together: the ``False`` return tells every caller to leave
        the Paperclip row standing, which makes the half-install a visible,
        deletable agent in Teams rather than a ghost. Paired with the
        sidecar-written-before-pairing above, the retry either resolves to that
        same live install or mints cleanly once the agents.list cross-check
        proves the id dead.

        Otherwise, both operations below are idempotent and best-effort: a
        missing entry / dir is a no-op, and a failure here is logged but
        never raised — the caller must surface the ORIGINAL deploy error,
        not a cleanup error. Removing the openclaw.json entry lets the
        container's file watcher drop the now-orphaned agent from memory on
        its next reload.
        """
        if isinstance(cause, TimeoutError):
            logger.info(
                "unwind_partial_deploy: skipping EFS/openclaw.json unwind for agent %s owner %s "
                "— cause was a client-side TimeoutError, the container may still be mid-write",
                new_agent_id,
                owner_id,
            )
            return False

        # Read the sidecar BEFORE unlinking it: it records the cron job ids
        # this deploy appended, and the unwind below is their only removal
        # path — a stranded job would keep firing against a dead agent, and
        # the retry would append duplicates beside it.
        cron_job_ids: list[str] = []
        try:
            sidecar = self._workspace.read_template_sidecar(owner_id, new_agent_id) or {}
            cron_job_ids = [j for j in (sidecar.get("cron_job_ids") or []) if isinstance(j, str)]
        except Exception:
            logger.exception(
                "unwind_partial_deploy: failed to read sidecar cron ids for agent %s owner %s",
                new_agent_id,
                owner_id,
            )

        # FIRST — see the docstring. Unlinking one file is what stops a
        # half-failed unwind from handing the next Hire a dead agent id, so its
        # success is what makes the unwind safe to report as such.
        sidecar_removed = True
        try:
            self._workspace.remove_template_sidecar(owner_id, new_agent_id)
        except Exception:
            sidecar_removed = False
            logger.exception(
                "unwind_partial_deploy: failed to remove .template sidecar for agent %s owner %s "
                "— reporting an UNSAFE unwind so the caller keeps the Paperclip row",
                new_agent_id,
                owner_id,
            )

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

        if cron_job_ids:
            from core.services.config_patcher import remove_cron_jobs

            try:
                await remove_cron_jobs(owner_id, cron_job_ids)
            except Exception:
                logger.exception(
                    "unwind_partial_deploy: failed to remove cron jobs %s for agent %s owner %s",
                    cron_job_ids,
                    new_agent_id,
                    owner_id,
                )

        try:
            # cleanup_deleted_agent runs the sync EFS rmtree on a worker thread —
            # those dirs
            # that can be large enough to block the event loop and stall
            # unrelated WS chat traffic, so run it on a worker thread — same
            # posture as the agents.delete path in websocket_chat.py.
            from core.containers.workspace import cleanup_deleted_agent

            await cleanup_deleted_agent(owner_id, new_agent_id, workspace=self._workspace)
        except Exception:
            logger.exception(
                "unwind_partial_deploy: failed to clean EFS dirs for agent %s owner %s",
                new_agent_id,
                owner_id,
            )

        return sidecar_removed

    async def _maybe_enable_acp(self, owner_id: str, manifest: dict) -> bool:
        """Turn on the buyer container's ACP coding-harness runtime when the
        deployed listing declares ``requires_acp``.

        Returns True iff the ACP block was written. Returns False (no-op) when:
          - the listing doesn't require ACP,
          - the buyer's provider can't authenticate any harness
            (``derive_enabled_harnesses`` returns []; notably every
            ``chatgpt_oauth`` owner, plus Gemini-only ones).

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

    def live_install_for_agent(self, owner_id: str, agent_id: str) -> dict[str, Any] | None:
        """Return a committed container install for ``agent_id``, if one exists.

        The sidecar is written before the gateway registration, so neither it
        nor ``agents.list`` is sufficient alone. Their intersection is the
        durable boundary used by retrying deploy callers.
        """
        sidecar = self._workspace.read_template_sidecar(owner_id, agent_id)
        if not isinstance(sidecar, dict) or sidecar.get("install_state") != "complete":
            return None

        config = self._workspace.read_openclaw_config(owner_id) or {}
        registered = {agent.get("id") for agent in roster_entries(config)}
        if agent_id not in registered:
            return None

        return {
            "agent_id": agent_id,
            "template_slug": sidecar.get("template_slug"),
            "template_version": sidecar.get("template_version"),
            "listing_id": sidecar.get("listing_id"),
            "seller_id": sidecar.get("seller_id"),
        }

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
                        # Canonical ID is absent from legacy sidecars. Those
                        # installs remain usable for idempotency but are not
                        # strong enough evidence for Marketplace provenance.
                        "listing_id": sidecar.get("listing_id"),
                        # None for legacy sidecars written before the field
                        # existed — install idempotency falls back to
                        # slug-only matching for those.
                        "seller_id": sidecar.get("seller_id"),
                    }
                )
        return deployed


_catalog_service: CatalogService | None = None


def get_catalog_service() -> CatalogService:
    """Returns the singleton CatalogService used by marketplace deploys.

    No longer reads from the admin-catalog S3 bucket — publish + unpublish
    moved to the marketplace flow. The remaining surface is
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
