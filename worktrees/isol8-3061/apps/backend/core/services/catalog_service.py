"""Catalog service — the marketplace install.

Publish + unpublish moved to the marketplace flow (see
``routers.marketplace_listings`` + ``core.services.marketplace_service``).
What's left is ``deploy_from_artifact``: mint the buyer's Paperclip agent when the
caller has not already, write its Hermes profile, and land the seller's tarball in
that profile's S3 prefix. Called by /marketplace/listings/{slug}/deploy.

Marketplace completion provenance is written by the storefront and approval callers after
their full rollback envelopes; this service only installs profile content.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

from core.config import settings
from core.observability.product_analytics import capture_product_event
from core.services import agent_export
from core.services.hermes_profiles import BACKEND_OWNED_FILES
from core.services.paperclip_credential_rule import PAPERCLIP_CREDENTIAL_PROHIBITION, PAPERCLIP_CREDENTIAL_RULE

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# AgentCore (Hermes) buyers: deploy writes into the S3 profile prefix, not EFS.
# ---------------------------------------------------------------------------


def _s3_client():
    import boto3

    return boto3.client("s3")


# Names deploy_tarball_to_profile must never write into a buyer's Hermes
# profile. Task 17's profile/config writer owns creating these for a FRESH
# agent; overwriting them with whatever rode along in the seller's tarball
# would clobber the buyer's own provider wiring. ``.env``/``config.yaml``
# should never be in a marketplace tarball in the first place (agent_export's
# dotfile filter already drops them) -- named here defensively so a future
# export bug can't turn into an overwrite here too.
#
# F3 (predeploy review, M6-R36): the ONE backend-owned file set
# (hermes_profiles.BACKEND_OWNED_FILES) -- before this fix this list carried
# only two of the five, so a tarball containing SOUL.md or lightbulb-facts.json
# (agent_export's filter drops neither) silently overwrote the profile
# writer's own copy in deploy_tarball_to_profile.
_DEPLOY_PROFILE_SKIP_NAMES = BACKEND_OWNED_FILES | {".marketplace-install.json"}


def _ensure_credential_rule(root: Path) -> None:
    """Append the credential prohibition to the artifact's ``AGENTS.md``, in the extracted
    tree, before it is uploaded.

    The seller's AGENTS.md is frozen in an immutable artifact, so a listing published before
    the rule existed -- or any seller who never wrote one -- would otherwise leave this agent
    with no prohibition while Paperclip's wake text still tells it to load the claimed-key file
    from disk. That is the 2026-08-14 leak: the model echoed a live token into its visible
    reasoning. Additive and idempotent (skipped when the prohibition is already present), and
    applied to the extracted COPY -- the seller's artifact is never rewritten in place.
    """
    target = root / "AGENTS.md"
    try:
        body = target.read_text(encoding="utf-8") if target.is_file() else ""
        if PAPERCLIP_CREDENTIAL_PROHIBITION in body:
            return
        target.write_text(
            (body.rstrip("\n") + "\n" + PAPERCLIP_CREDENTIAL_RULE) if body else PAPERCLIP_CREDENTIAL_RULE,
            encoding="utf-8",
        )
    except OSError:
        logger.exception("marketplace deploy: could not append the credential rule to AGENTS.md")


async def deploy_tarball_to_profile(*, owner_id: str, agent_id: str, tar_bytes: bytes, container: dict) -> None:
    """Extract ``tar_bytes`` into the buyer's Hermes profile prefix (S3,
    ``<owner>/hermes/<agent_id>/``) instead of their EFS
    ``workspace-{agent_id}/`` dir.

    Skips ``.env``/``config.yaml`` — the profile writer (Task 17) owns
    creating those for a freshly deployed agent. Writes a fresh ``.revision``
    marker LAST so the bridge re-pulls the new content on its next turn
    (M4-R20) — writing it before the content above would let a concurrent
    restore see a bumped revision with only some files present.

    Agent registration (the Paperclip mint) and the agent's Hermes profile
    (config.yaml/.env/SOUL.md) are NOT done here — see ``_install_agentcore_deploy``,
    which calls this function only after both of those exist, so this is
    purely "place the artifact's content files" (fix round 1, M6-R9).

    ``container`` is unused — every write targets the S3 profile. It is accepted so the
    caller does not have to drop an argument it already has in hand.
    """

    def _write() -> None:
        # Fix round 1, item 4: OWNER_STATE_BUCKET is the profile writer's
        # (hermes_profiles.upload_profile) single source of truth for this bucket.
        bucket = settings.OWNER_STATE_BUCKET
        s3 = _s3_client()
        prefix = f"{owner_id}/hermes/{agent_id}"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            if tar_bytes:
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
                    tf.extractall(tmp_path, filter="data")
                _ensure_credential_rule(tmp_path)
                for f in sorted(tmp_path.rglob("*")):
                    if not f.is_file() or f.name in _DEPLOY_PROFILE_SKIP_NAMES:
                        continue
                    rel = f.relative_to(tmp_path).as_posix()
                    s3.upload_file(str(f), bucket, f"{prefix}/{rel}")
            revision = uuid.uuid4().hex
            marker = tmp_path / ".revision"
            marker.write_text(revision, encoding="utf-8")
            s3.upload_file(str(marker), bucket, f"{prefix}/.revision")

    await asyncio.to_thread(_write)


async def _resolve_agent_row_for_profile(owner_id: str, agent_id: str) -> dict:
    """The Paperclip roster entry shape ``write_agent_profile`` wants
    (``{"id","name","role","title","capabilities","icon"}``).

    Fix round 3 (M6-R10): hard-fails like ``hermes_provisioning.write_profiles``
    does on its own roster read (no try/except there either, and it never
    looks for one specific id — it just iterates whatever the roster returns)
    — the agent was just minted, so a roster read failure, or a roster that
    doesn't (yet) carry the just-minted id, is a real failure to surface, not
    a cosmetic one to paper over with a manifest-derived stand-in row.
    """
    from core.services.messaging import load_agent_roster

    roster = await load_agent_roster(owner_id)
    entry = next((a for a in roster if isinstance(a, dict) and a.get("id") == agent_id), None)
    if entry is None:
        raise RuntimeError(f"hermes profile for owner={owner_id} agent={agent_id}: not found in the Paperclip roster")
    return entry


async def _build_and_upload_agent_profile(
    *, owner_id: str, agent_id: str, container: dict, soul_md_override: str | None = None
) -> None:
    """The Hermes-profile half of an AgentCore install: config.yaml/.env/SOUL.md
    for the freshly-registered agent.

    M6-R23: a thin resolve-then-delegate wrapper. The actual write (owner-level
    inputs via ``resolve_profile_inputs``, this agent's Paperclip API key mint,
    ``write_agent_profile``, ``upload_profile``) moved to
    ``hermes_provisioning.write_profile_for_agent`` -- the SAME per-agent seam
    ``hermes_provisioning.write_profiles`` (provision time, whole starter
    roster) and ``runtime_rpc``'s ``agents.create`` door now share, so there is
    exactly one place that sources those inputs (see that function's docstring
    for the three ways this file used to diverge before the consolidation).
    Only the per-agent roster-row resolution — this agent's own entry, by id —
    stays here: the caller only has ``agent_id``, unlike ``write_profiles``
    (which already holds each row from its own roster iteration).

    Uploaded (and its own ``.revision`` written) BEFORE
    ``deploy_tarball_to_profile`` runs, so the newly-minted agent has a
    complete, bootable profile even before its marketplace content lands.

    ``soul_md_override`` (M6-R41): the seller's own SOUL.md text, read out of
    the marketplace artifact by ``_install_agentcore_deploy`` -- threaded
    straight through to ``write_profile_for_agent``'s own parameter of the
    same name. ``None`` (no SOUL.md in the tarball) keeps the roster-derived
    default.
    """
    from core.services.hermes_provisioning import write_profile_for_agent

    agent_row = await _resolve_agent_row_for_profile(owner_id, agent_id)
    await write_profile_for_agent(owner_id, agent_row, soul_md_override=soul_md_override)


def _read_soul_md_from_tar(tar_bytes: bytes) -> str | None:
    """M6-R41: the seller's own ``SOUL.md``, read out of the marketplace artifact BEFORE the
    profile writer runs -- the OpenClaw workspace's ``SOUL.md`` IS the agent's identity (the ECS
    install path already ships it verbatim, untouched, via the buyer's EFS extract), and F3's
    widened ``_DEPLOY_PROFILE_SKIP_NAMES`` means ``deploy_tarball_to_profile``'s own later
    content pass now skips it -- without this, an AgentCore install would silently fall back to
    the roster-derived identity instead of the seller's own persona/instructions.

    ``None`` when the tarball is empty or carries no ``SOUL.md`` -- callers pass that straight
    through as ``write_profile_for_agent``'s own ``soul_md_override``, whose ``None`` default
    keeps producing the roster-derived identity, unchanged.
    """
    if not tar_bytes:
        return None
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and Path(member.name).name == "SOUL.md":
                extracted = tf.extractfile(member)
                if extracted is None:
                    return None
                return extracted.read().decode("utf-8", errors="replace")
    return None


async def _install_agentcore_deploy(
    *,
    owner_id: str,
    slug: str,
    manifest: dict,
    tar_bytes: bytes,
    container: dict,
    new_agent_id: str | None,
    listing_id: str | None,
    seller_id: str | None,
) -> dict[str, Any]:
    """The AgentCore transport's substitute for the ECS branch's end-to-end
    install (Paperclip mint -> gateway pairing) — fix round 1, M6-R9.

    AgentCore has no gateway for ``pair_with_openclaw`` to register against,
    so the registration step for a caller that hasn't already minted a
    Paperclip agent (``new_agent_id is None`` — the same "direct/Custom"
    contract the ECS branch supports via its own local-uuid fallback) is to
    mint one through ``create_paperclip_for_marketplace`` — the SAME helper
    every pre-minting caller (marketplace deploy, hire, agent builder) already
    uses, whose ``resolve_agent_adapter`` call already picks the ``http``
    adapter for an AgentCore row. A caller that already minted (every
    production caller today) is NOT re-minted here — Paperclip assigns its
    own id at creation, so "creating another one" would just orphan a second
    agent under a different id than the one this call was asked to deploy.

    Order matters: profile (config.yaml/.env/SOUL.md, ending in the writer's
    OWN ``.revision``) BEFORE content (the marketplace tarball, ending in ITS
    ``.revision`` — the actually-authoritative one, since it lands last). A
    bridge that re-pulls between the two steps still finds a complete,
    bootable profile; reversing the order could momentarily advertise
    marketplace content with no profile underneath it.

    M6-R41: the profile's SOUL.md is the SELLER's own, read out of the tarball
    (``_read_soul_md_from_tar``) and threaded into the profile write as
    ``soul_md_override`` -- ``deploy_tarball_to_profile``'s later content pass
    skips SOUL.md (F3/M6-R36), so without this the profile write's own
    roster-derived default would silently win instead of the seller's actual
    persona/instructions (the ECS install path already ships this file
    verbatim via the buyer's EFS extract; this is the AgentCore equivalent).
    ``None`` when the tarball carries no SOUL.md -- unchanged, roster-derived
    identity.

    Rollback: only the Paperclip agent THIS call minted is torn down on
    failure — a pre-minted agent is the caller's own rollback envelope
    (mirrors every ECS caller's existing ``unwind``/``_rollback_agent``).
    """
    from core.services.paperclip_agent_idempotency import build_agent_create_idempotency_key
    from core.services.paperclip_agent_lifecycle import (
        create_paperclip_for_marketplace,
        delete_paperclip_for_marketplace,
    )

    minted_new_agent = new_agent_id is None
    if minted_new_agent:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=owner_id,
            owner_id=owner_id,
            name=manifest.get("name", slug),
            role=None,
            idempotency_key=build_agent_create_idempotency_key(
                operation="marketplace-deploy-agentcore",
                owner_id=owner_id,
                intent={"slug": slug, "listingId": listing_id},
            ),
        )
        new_agent_id = paperclip_agent["id"]

    try:
        soul_md = _read_soul_md_from_tar(tar_bytes)
        await _build_and_upload_agent_profile(
            owner_id=owner_id, agent_id=new_agent_id, container=container, soul_md_override=soul_md
        )
        await deploy_tarball_to_profile(
            owner_id=owner_id, agent_id=new_agent_id, tar_bytes=tar_bytes, container=container
        )
    except Exception:
        if minted_new_agent:
            try:
                await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
            except Exception:
                logger.exception(
                    "agentcore deploy: rollback delete_paperclip_for_marketplace failed for agent %s", new_agent_id
                )
        raise

    return {
        "slug": slug,
        "version": manifest.get("version", 1),
        "agent_id": new_agent_id,
        "name": manifest.get("name", slug),
        "origin": "Marketplace" if listing_id and seller_id else "Custom",
    }


class CatalogService:
    """Stateless. It kept three injected EFS/gateway collaborators (a ``Workspace``, the
    openclaw.json aux merger, the gateway pair fn) and none has a successor -- the install is
    two S3 writes now. The class stays so ``get_catalog_service()`` keeps its one seam."""

    async def deploy_from_artifact(
        self,
        *,
        owner_id: str,
        slug: str,
        manifest: dict,
        slice_: dict,
        tar_bytes: bytes,
        new_agent_id: str | None = None,
        listing_id: str | None = None,
        seller_id: str | None = None,
        container: dict | None = None,
    ) -> dict[str, Any]:
        """Deploy a pre-fetched artifact (tarball + slice + manifest) into the buyer's
        Hermes profile.

        ``container`` (the buyer's row) is an OPTIMISATION for callers that already hold it,
        not a decision: ``None`` is read here. It is required because the profile write needs
        the owner's runtime identity.

        Steps, in order:
          1. **Credential gate.** The buyer must have connected every provider the agent hard
             depends on (declared in the manifest or implied by the bundled MCP servers);
             otherwise the agent installs and immediately fails on its first credentialed call.
             Runs before ANY side effect so a blocked deploy leaves no partial state.
          2. **Strip the tarball's boot vectors.** A seller artifact can carry files that
             execute at agent boot (``BOOT.md``, ``openclaw.plugin.json``, a ``package.json``
             with ``openclaw.extensions``); they are removed before the bytes reach the
             buyer's profile.
          3. ``_install_agentcore_deploy``: mint the Paperclip agent when the caller has not,
             write the profile (config.yaml/.env/SOUL.md), then upload the stripped tarball's
             content.

        Args:
            new_agent_id: the buyer-side agent id. Marketplace deploys pass Paperclip's
                freshly-minted UUID so the two systems share the same id. ``None`` means
                "mint one" -- through Paperclip, never a local string, so a direct caller
                cannot orphan an id Paperclip does not know.
            listing_id / seller_id: canonical marketplace identity, supplied by the trusted
                listing row and never the seller artifact. Direct callers such as the agent
                builder omit them and therefore remain Custom.

        Rollback posture: a failed step propagates to the caller, which owns the deploy
        transaction. Only the Paperclip agent THIS call minted is torn down here; a
        pre-minted agent is the caller's own rollback envelope.
        """
        if container is None:
            from core.repositories import container_repo

            container = await container_repo.get_by_owner_id(owner_id) or {}
        await assert_credentials_satisfied(owner_id, manifest, slice_)
        return await _install_agentcore_deploy(
            owner_id=owner_id,
            slug=slug,
            manifest=manifest,
            tar_bytes=_strip_plugin_boot_vectors(tar_bytes, slug=slug),
            container=container,
            new_agent_id=new_agent_id,
            listing_id=listing_id,
            seller_id=seller_id,
        )


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

    _catalog_service = CatalogService()
    return _catalog_service
