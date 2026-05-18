"""Snapshot a seller's OpenClaw agent from EFS into a marketplace artifact.

Path B for the marketplace publish flow: an Isol8 paid user picks one of
their existing agents and we tar the agent's EFS directory directly into a
CatalogPackage with format="openclaw". No container interaction — we read
the agents/ tree even when the container is scaled to zero.

Snapshot at call time. Subsequent edits to the seller's agent do not affect
already-published versions; the buyer always installs the bytes that were
in S3 at upload time.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import re
import tarfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.observability.metrics import put_metric
from core.services import marketplace_safety
from core.services.catalog_package import CatalogPackage
from core.services.catalog_slice import (
    classify_skill,
    extract_agent_slice,
    filter_cron_jobs_for_agent,
)


# Persona files (per upstream openclaw/src/agents/workspace.ts) — these get
# format="persona" scans (email/phone patterns) on top of the credential
# patterns. Everything else MD-shaped gets format="skillmd".
_PERSONA_FILES = frozenset({"IDENTITY.md", "SOUL.md", "USER.md", "BOOTSTRAP.md", "MEMORY.md", "HEARTBEAT.md"})

logger = logging.getLogger(__name__)

# Friday is the per-owner CEO agent seeded by friday_identity.py — its
# IDENTITY/SOUL/USER files name the owner and its bootstrap is wired into
# Paperclip's CEO slot. Publishing it would (a) leak owner-keyed seeding
# content and (b) hand a buyer a clone of someone else's CEO. The router
# 400s when this guard returns True.
FRIDAY_PUBLISH_ERROR_MSG = "cannot publish your default agent; only custom agents can be listed in the marketplace"


_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")


# Top-level dirs we never want to ship — caches, transient state, VCS metadata.
_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".cache",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
        ".DS_Store",
    }
)


# Filename-based credential-leak guard. Defense in depth on top of
# marketplace_safety's content scan — if a file looks like credential
# material by SHAPE, we skip it at tar time regardless of contents.
#
# Three buckets, all checked against the basename only:
#
# 1. Exact-match SSH key files. These have no extension we can key on, so
#    we hardcode the canonical names.
_EXACT_CREDENTIAL_FILENAMES = frozenset(
    {
        "id_rsa",
        "id_rsa.pub",
        "id_dsa",
        "id_dsa.pub",
        "id_ecdsa",
        "id_ecdsa.pub",
        "id_ed25519",
        "id_ed25519.pub",
        "known_hosts",
        "authorized_keys",
    }
)

# 2. Extensions that are almost always private-key / cert material.
_CREDENTIAL_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx"})

# 3. Config-shape extensions where the STEM signals secret content. Match
#    a credentials/secrets/api-key/private-key token at a word boundary
#    (start, end, or surrounded by `.`/`-`/`_`). Earlier we substring-
#    matched the basename which would drop legitimate workspace files like
#    `skills/secretary.md`, `skills/secret-santa.md`, or
#    `credentials-helper.ts` (Codex P2 on PR #710, commit 1f30aa3c).
_CREDENTIAL_TOKEN_RE = re.compile(
    r"(?:^|[._\-])(credentials?|secrets?|private[._\-]?keys?|api[._\-]?keys?)(?:[._\-]|$)",
    re.IGNORECASE,
)
_CREDENTIAL_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".env", ".toml", ".ini", ".cfg", ".conf"})


def _looks_like_credential_file(name: str) -> bool:
    """Filename-shape check. Returns True for files we should never ship.

    Catches: id_rsa, *.pem, *.key, *.p12, aws.credentials.json,
    resy-credentials.json, openai.secret.json, service-account-secrets.yaml.

    Does NOT catch: skills/secretary.md, skills/secret-santa.md,
    credentials-helper.ts — the stem has to carry the credential token as
    a WHOLE segment and the file has to use a config-shape extension.
    """
    lower = name.lower()
    if lower in _EXACT_CREDENTIAL_FILENAMES:
        return True
    # rpartition gives ("stem", ".", "ext") or ("", "", "name") for
    # extensionless files. We only care about the extension half.
    stem, dot, ext = lower.rpartition(".")
    if not dot:
        return False
    ext_with_dot = f".{ext}"
    if ext_with_dot in _CREDENTIAL_EXTENSIONS:
        return True
    if ext_with_dot in _CREDENTIAL_CONFIG_EXTENSIONS and _CREDENTIAL_TOKEN_RE.search(stem):
        return True
    return False


class AgentNotFoundError(Exception):
    """Seller has no agent workspace at workspaces/{agent_id} on EFS."""


class InvalidAgentIdError(Exception):
    """agent_id failed format validation or path-traversal guard."""


class AgentNotInConfigError(Exception):
    """agent_id has a workspace dir on EFS but no entry in the seller's openclaw.json
    agents.list — can't synthesize a slice without the agent's identity/skills/tools.
    """


def _resolve_agent_dir(seller_id: str, agent_id: str) -> Path:
    """Resolve the seller's agent WORKSPACE directory on EFS.

    The OpenClaw EFS layout splits each agent across two sibling roots
    under the seller's user dir:

      agents/{id}/      — OpenClaw runtime state (sessions/ logs,
                          agent/ auth tokens). Internal; not seller content.
      workspaces/{id}/  — the seller's actual workspace: persona files
                          (IDENTITY.md / SOUL.md / USER.md / HEARTBEAT.md /
                          TOOLS.md), bundled skills (skills/), MCP server
                          code, memory, etc. This is what buyers need.

    The export tars the WORKSPACE dir. The earlier implementation pointed
    at agents/{id}/, which contained only OpenClaw's internal state — so
    the resulting tarball was either runtime junk (pre-PR #687) or empty
    (post-#687 filter). Buyers got nothing useful. Fix is to point at
    workspaces/{id}/; the existing filter list strips node_modules, .git,
    dotfiles, etc. so the published artifact stays clean.

    Emits ``marketplace.path_traversal_attempt`` and raises
    InvalidAgentIdError on any traversal attempt.
    """
    if not _AGENT_ID_RE.match(agent_id):
        raise InvalidAgentIdError(f"agent_id must be alphanumeric (with . _ -), 3-64 chars: {agent_id!r}")

    workspace = get_workspace()
    try:
        seller_root = workspace.user_path(seller_id).resolve()
    except WorkspaceError as exc:
        raise InvalidAgentIdError(str(exc)) from exc

    workspaces_root = seller_root / "workspaces"
    candidate = (workspaces_root / agent_id).resolve()

    try:
        candidate.relative_to(workspaces_root.resolve())
    except ValueError:
        put_metric("marketplace.path_traversal_attempt")
        raise InvalidAgentIdError(f"agent_id resolves outside seller's workspaces directory: {agent_id!r}")

    return candidate


def _build_tarball(agent_dir: Path) -> tuple[bytes, list[str]]:
    """Tar (gzip) an agent directory, skipping junk dirs and dotfiles at the
    top level. File mtimes are zeroed so two snapshots of unchanged content
    produce identical bytes (deterministic SHA-256).
    """
    if not agent_dir.exists() or not agent_dir.is_dir():
        raise AgentNotFoundError(f"agent dir not found: {agent_dir}")

    contents: list[str] = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:

        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            # Skip junk dirs anywhere in the tree.
            parts = tarinfo.name.split("/")
            if any(p in _SKIP_DIR_NAMES for p in parts):
                return None
            # Skip hidden files / hidden dirs anywhere in the tree. The
            # leading "." in tarinfo.name is the arcname root (we pass
            # arcname="."), so ignore it; any other path segment starting
            # with "." is a dotfile/dotdir. Without this, .env, .ssh/*,
            # .aws/credentials, .openclaw/secrets — anything a seller had
            # in their workspace — would ship in the marketplace artifact.
            for p in parts:
                if p in (".", ""):
                    continue
                if p.startswith("."):
                    return None
            # Filename-based credential-leak guard. The seller may have
            # plopped a credentials JSON (resy-credentials.json,
            # aws.credentials.json, *.secret.json, id_rsa, etc.) into their
            # workspace. marketplace_safety.scan also greps content for
            # credential patterns, but a filename-level skip is cheap
            # defense in depth — if the file matches a credential shape,
            # we never tar it in the first place.
            basename = parts[-1]
            if _looks_like_credential_file(basename):
                return None
            # Reject symlinks defensively (catalog_package.untar already does
            # the receiving-side check, but rejecting here keeps the artifact
            # auditable).
            if tarinfo.issym() or tarinfo.islnk():
                return None
            # Determinism: zero mtimes + uid/gid so identical content → identical bytes.
            tarinfo.mtime = 0
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            if not tarinfo.isdir():
                contents.append(tarinfo.name)
            return tarinfo

        # arcname="." gives us paths like "./openclaw.json" — same convention
        # as catalog_package.tar_directory uses for catalog uploads.
        tf.add(str(agent_dir), arcname=".", filter=_filter)

    return buf.getvalue(), sorted(contents)


def _summarize_openclaw_config(agent_dir: Path) -> dict:
    """Extract a small summary from openclaw.json if present.

    Best-effort; returns minimal dict on missing / invalid file.
    """
    cfg_path = agent_dir / "openclaw.json"
    if not cfg_path.exists():
        return {"name": agent_dir.name, "description": ""}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": agent_dir.name, "description": ""}

    identity = data.get("identity", {}) if isinstance(data, dict) else {}
    return {
        "name": identity.get("name") or agent_dir.name,
        "description": identity.get("description") or identity.get("vibe") or "",
    }


def _scan_tarball_for_safety_flags(tarball_bytes: bytes) -> list[dict]:
    """Walk the snapshot tarball, partition files by safety-scan format
    (persona / openclaw / skillmd), run each bucket, return the union as
    JSON-shaped dicts ready for the manifest.

    Trust-seller posture: this surfaces findings inline in the wizard so
    the seller can review + scrub before submit. Nothing here redacts.
    Non-text / binary files are skipped (the scanner's binary heuristic
    + size cap handles them).
    """
    persona_files: dict[str, bytes] = {}
    openclaw_files: dict[str, bytes] = {}
    skillmd_files: dict[str, bytes] = {}

    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            data = fobj.read()
            basename = name.rsplit("/", 1)[-1]
            if basename in _PERSONA_FILES:
                persona_files[name] = data
            elif basename.endswith(".json") and basename == "openclaw.json":
                openclaw_files[name] = data
            elif basename.endswith(".md"):
                skillmd_files[name] = data
            # Other file types skipped — scanner is regex/text-only.

    flags: list[marketplace_safety.SafetyFlag] = []
    if persona_files:
        flags.extend(marketplace_safety.scan(persona_files, format="persona"))
    if openclaw_files:
        flags.extend(marketplace_safety.scan(openclaw_files, format="openclaw"))
    if skillmd_files:
        flags.extend(marketplace_safety.scan(skillmd_files, format="skillmd"))

    return [dataclasses.asdict(f) for f in flags]


def export_agent_from_efs(
    seller_id: str,
    agent_id: str,
    *,
    selected_mcps: list[str] | None = None,
    selected_skills: list[str] | None = None,
) -> CatalogPackage:
    """Read the seller's agent dir from EFS, tar it, return a CatalogPackage.

    The CatalogPackage carries an ``openclaw_slice`` synthesized from the
    seller's per-user openclaw.json so the buyer-side deploy can register
    the new agent in their config (matching the admin-catalog publish
    behavior). Without the slice, deploy_from_artifact extracts files but
    leaves the buyer's openclaw.json untouched.

    When ``selected_mcps`` / ``selected_skills`` are provided (from the
    publish wizard's Bundle step), the slice also carries the matching
    ``mcp.servers`` / ``skills.entries`` / ``skills.allowBundled`` blocks
    with secret fields scrubbed to ``""``. ``None`` for either keeps the
    legacy behavior (no MCPs/skills bundled).

    The manifest carries a ``safety_flags`` list populated by scanning
    persona / md / openclaw.json files against ``marketplace_safety`` —
    the wizard's Review step surfaces these inline so the seller can
    edit + re-snapshot before submit. The seller is the gatekeeper;
    nothing here redacts.

    Args:
        seller_id: Clerk user id of the seller (also the EFS dir owner).
        agent_id: Subdirectory name under ``agents/`` to publish.
        selected_mcps: Names of MCP servers to bundle (from publish wizard).
        selected_skills: Names of skills to bundle (from publish wizard).

    Raises:
        InvalidAgentIdError: agent_id failed format / path-traversal check.
        AgentNotFoundError: agent_id not found on EFS for this seller.
        AgentNotInConfigError: agent dir exists but agent_id isn't registered
            in the seller's openclaw.json (or the config file is missing).
    """
    agent_dir = _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id)
    tarball_bytes, contents = _build_tarball(agent_dir)

    workspace = get_workspace()
    config = workspace.read_openclaw_config(seller_id)
    if not config:
        raise AgentNotInConfigError(
            f"seller {seller_id!r} has no openclaw.json — publish the agent from a container that has been provisioned"
        )
    workspace_skill_names = _enumerate_workspace_skill_names(agent_dir)
    try:
        slice_ = extract_agent_slice(
            config,
            agent_id,
            selected_mcps=selected_mcps,
            selected_skills=selected_skills,
            workspace_skill_names=workspace_skill_names,
        )
    except KeyError as exc:
        raise AgentNotInConfigError(
            f"agent {agent_id!r} is not registered in seller's openclaw.json agents.list"
        ) from exc
    slice_["cron_jobs"] = filter_cron_jobs_for_agent(workspace.read_cron_jobs(seller_id), agent_id)

    safety_flags = _scan_tarball_for_safety_flags(tarball_bytes)

    summary = _summarize_openclaw_config(agent_dir)
    manifest = {
        "name": summary["name"],
        "description": summary["description"],
        "format": "openclaw",
        "exported_at": int(time.time()),
        "agent_id": agent_id,
        "file_count": len(contents),
        "safety_flags": safety_flags,
    }

    return CatalogPackage(
        format="openclaw",
        manifest=manifest,
        openclaw_slice=slice_,
        tarball_bytes=tarball_bytes,
        tarball_contents=contents,
    )


# ---------------------------------------------------------------------------
# Publish-wizard dry-run preview
# ---------------------------------------------------------------------------


def _enumerate_workspace_skill_names(agent_dir: Path) -> set[str]:
    """Names of skill files/dirs directly under ``agent_dir/skills/``.

    OpenClaw's loader picks up skill files via ``skills.load.extraDirs``;
    when a seller drops a `.md`/`.ts` skill file into their agent's
    workspace ``skills/`` subdir, those names live "in the workspace"
    (bucket WORKSPACE in the classifier). We strip suffixes so the
    classifier matches `agent.skills` entries that omit the extension.
    """
    skills_root = agent_dir / "skills"
    if not skills_root.exists() or not skills_root.is_dir():
        return set()
    names: set[str] = set()
    for entry in skills_root.iterdir():
        if entry.name.startswith("."):
            continue
        # File: strip the suffix. Dir: take the name as-is.
        if entry.is_file():
            names.add(entry.stem)
        else:
            names.add(entry.name)
    return names


def _enumerate_persona_files(agent_dir: Path) -> list[str]:
    """Return persona-file basenames actually present in ``agent_dir``.

    Used by the publish wizard's Bundle step to show the seller exactly
    which persona files will ship in the tarball. v1 ships all of them
    unconditionally — this is informational.
    """
    return sorted(name for name in _PERSONA_FILES if (agent_dir / name).is_file())


def enumerate_for_preview(*, seller_id: str, agent_id: str) -> dict:
    """Build the dry-run preview the publish wizard renders before submit.

    Performs a real snapshot (tar + scan) so ``safety_flags`` reflect the
    seller's actual workspace bytes — selection (which MCPs/skills to
    include) doesn't affect the scan result, so the frontend can cache
    this once and reuse across Bundle/Review steps.

    Raises the same errors as ``export_agent_from_efs``:
    InvalidAgentIdError / AgentNotFoundError / AgentNotInConfigError.
    """
    agent_dir = _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id)
    workspace = get_workspace()
    config = workspace.read_openclaw_config(seller_id) or {}

    # Walk agent.skills to get the seller's per-agent skill list; classify
    # each against the full container config + workspace.
    agents_list = (config.get("agents") or {}).get("list") or []
    agent_entry = next(
        (a for a in agents_list if isinstance(a, dict) and a.get("id") == agent_id),
        None,
    )
    if not isinstance(agent_entry, dict):
        raise AgentNotInConfigError(f"agent {agent_id!r} is not registered in seller's openclaw.json agents.list")

    workspace_skill_names = _enumerate_workspace_skill_names(agent_dir)
    raw_skills = agent_entry.get("skills") or []
    skill_names: list[str] = []
    if isinstance(raw_skills, list):
        skill_names = [s for s in raw_skills if isinstance(s, str)]
    elif isinstance(raw_skills, dict):  # {list: [...]} shape
        nested = raw_skills.get("list") or []
        skill_names = [s for s in nested if isinstance(s, str)]

    available_skills = [
        {
            "name": name,
            "bucket": classify_skill(
                name,
                config,
                workspace_skill_names=workspace_skill_names,
            ).value,
        }
        for name in skill_names
    ]

    mcp_servers = (config.get("mcp") or {}).get("servers") or {}
    available_mcps = sorted(mcp_servers.keys()) if isinstance(mcp_servers, dict) else []

    persona_files = _enumerate_persona_files(agent_dir)

    # Build the tarball + scan for safety_flags. We don't return the
    # tarball itself — just the flags.
    tarball_bytes, _ = _build_tarball(agent_dir)
    safety_flags = _scan_tarball_for_safety_flags(tarball_bytes)

    return {
        "agent_id": agent_id,
        "available_mcps": available_mcps,
        "available_skills": available_skills,
        "persona_files": persona_files,
        "safety_flags": safety_flags,
    }


# ---------------------------------------------------------------------------
# Friday-publish guard
# ---------------------------------------------------------------------------


async def _default_fetch_paperclip_agents(*, owner_id: str) -> list[dict]:
    """Production-path Paperclip agent fetch — constructs the httpx client,
    repo, and admin_client the same way ``ecs_manager`` does, then delegates
    to ``fetch_paperclip_agents_for_owner``. Returns ``[]`` if anything
    fails (network, auth, Paperclip down) so callers degrade gracefully
    rather than blocking publishes when Paperclip is unavailable.
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_openclaw_pairing import (
        fetch_paperclip_agents_for_owner,
    )

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        admin_client = PaperclipAdminClient(http_client=http)
        return await fetch_paperclip_agents_for_owner(owner_id=owner_id, repo=repo, admin_client=admin_client)
    except Exception as exc:  # noqa: BLE001 — graceful degrade per docstring
        logger.warning(
            "agent_export: default paperclip fetch failed for owner=%s: %s",
            owner_id,
            exc,
        )
        return []
    finally:
        await http.aclose()


async def is_reserved_friday_agent(
    *,
    owner_id: str,
    agent_id: str,
    fetch_paperclip_agents: Callable[..., Awaitable[list[dict]]] | None = None,
) -> bool:
    """Return True iff ``agent_id`` is this owner's CEO (Friday) agent.

    Used by the marketplace publish path to refuse snapshotting Friday — see
    ``FRIDAY_PUBLISH_ERROR_MSG`` for the surfaced reason. Returns False
    (allow publish) when:
      - Paperclip returns no agents (provisioning hasn't run, or Paperclip
        is unreachable; same graceful-degrade posture as friday_identity).
      - No entry in the list carries ``role == "ceo"``.
      - The CEO entry's id doesn't match ``agent_id``.

    The fetcher dependency is injected so tests don't need a live Paperclip.
    Production callers can omit it and ``_default_fetch_paperclip_agents``
    is used.
    """
    fetch = fetch_paperclip_agents or _default_fetch_paperclip_agents
    agents = await fetch(owner_id=owner_id)
    if not agents:
        return False
    friday = next((a for a in agents if isinstance(a, dict) and a.get("role") == "ceo"), None)
    if friday is None:
        return False
    return friday.get("id") == agent_id
