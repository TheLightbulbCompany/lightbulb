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
import gzip
import io
import json
import logging
import os
import re
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.observability.metrics import put_metric
from core.services import marketplace_safety
from core.services.catalog_package import CatalogPackage
from core.services.catalog_slice import (
    _agents_list,
    classify_skill,
    extract_agent_slice,
    filter_cron_jobs_for_agent,
)
from core.services.inbound_triggers import automation_from_title
from core.services.marketplace.requirements import (
    build_manifest_requirements,
    load_effective_config,
    requirements_scan,
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

# Content-scan ceiling for the generic text-file bucket in
# _scan_tarball_for_safety_flags — anything larger is media/data, not config.
_SCAN_MAX_FILE_BYTES = 1 * 1024 * 1024


# Sentinel that replaces the seller's agent_id in MCP server args at
# export time. ``catalog_service.deploy_from_artifact`` substitutes this
# back to the buyer's freshly-minted agent_id before deep-merging the
# slice into their openclaw.json. Without this rewrite, the slice's args
# would still reference the seller's workspace path — which on the
# buyer's container doesn't exist (different agent_id).
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"


def _agent_id_from_dir_name(dir_name: str) -> str:
    """Strip the upstream per-agent workspace prefix from a dir name.

    Workspace dirs are ``workspace-{agent_id}`` (upstream convention); the
    agent id is everything after the prefix. Passing an already-bare id
    through is a no-op, so callers can use this on any resolved dir name.
    """
    prefix = "workspace-"
    return dir_name[len(prefix) :] if dir_name.startswith(prefix) else dir_name


def _rewrite_mcp_args_paths(slice_: dict, seller_agent_id: str) -> None:
    """In-place: replace ``workspace-{seller_agent_id}`` segments in MCP
    server args with ``workspace-{_AGENT_DIR_SENTINEL}``.

    Only ``args`` is rewritten; ``command`` and ``env`` don't carry
    workspace-relative paths in any seller config we've seen, but the
    function is defensive — adding more fields is a one-line change.
    """
    servers = ((slice_.get("mcp") or {}).get("servers")) or {}
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [
                # Old-form first: sellers whose containers predate the
                # workspace-layout migration still carry ``workspaces/{id}``
                # in their MCP args; normalize both spellings to the new
                # sentinel form so every artifact ships layout-current.
                a.replace(f"workspaces/{seller_agent_id}", f"workspace-{_AGENT_DIR_SENTINEL}").replace(
                    f"workspace-{seller_agent_id}", f"workspace-{_AGENT_DIR_SENTINEL}"
                )
                if isinstance(a, str)
                else a
                for a in args
            ]


# Top-level dirs we never want to ship — caches, transient state, VCS metadata.
#
# ``uploads`` is OpenClaw's per-workspace attachment/media staging dir: inbound
# files (photos, docs) sent through an agent's channels land there at runtime.
# It is user/runtime data, not part of the agent's portable definition (which is
# pipelines/scripts/skills/docs + openclaw.json), so it must never reach a
# buyer's workspace — shipping it both leaks the seller's users' attachments and
# drags along stale snapshots of files that live canonically elsewhere.
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
        "uploads",
    }
)


# File basenames we never ship, at any depth. Both are OpenClaw ingestion
# vectors on the buyer's side: a workspace ``openclaw.plugin.json`` makes the
# directory discoverable as loadable in-process plugin code, and a workspace
# ``BOOT.md`` triggers an autonomous LLM run on every gateway restart
# (internal hooks are enabled fleet-wide). Neither is part of an agent's
# portable definition. The deploy side (``catalog_service``) strips the same
# names from already-published artifacts, which predate this filter.
_SKIP_FILE_BASENAMES = frozenset({"BOOT.md", "openclaw.plugin.json"})


def package_json_declares_openclaw_extensions(raw: bytes) -> bool:
    """True when *raw* is a package.json that would register a workspace
    plugin — an ``openclaw.extensions`` declaration, nested
    (``{"openclaw": {"extensions": …}}``) or as the literal dotted key — or
    cannot be proven safe (malformed / non-object JSON → fail closed, since
    OpenClaw's own more forgiving parser might still read it).

    Shared by the export tar filter below and the deploy-side artifact strip
    (``catalog_service._strip_plugin_boot_vectors``) so the two sides can
    never disagree on what counts as a plugin manifest.
    """
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return True
    if not isinstance(parsed, dict):
        return True
    if "openclaw.extensions" in parsed:
        return True
    openclaw = parsed.get("openclaw")
    return isinstance(openclaw, dict) and "extensions" in openclaw


# Filename-based credential-leak guard. Defense in depth on top of
# marketplace_safety's content scan — if a file looks like credential
# material by SHAPE, we skip it at tar time regardless of contents.
#
# Three buckets, all checked against the basename only:
#
# 1. Exact-match filenames that are credential material with no useful
#    extension we can key on. SSH keys + the classic AWS-style
#    extensionless `credentials` / `secrets` profile files. Listed
#    explicitly so the extensionless-name branch in
#    ``_looks_like_credential_file`` blocks them while still letting
#    ``credentials-helper`` (an extensionless script, no AWS shape)
#    through. A naive substring filter catches these only by accident.
_EXACT_CREDENTIAL_FILENAMES = frozenset(
    {
        # SSH-style private keys.
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
        # AWS-style profile files (no extension by convention).
        "credentials",
        "credential",
        "secrets",
        "secret",
        # Private key + API key conventions seen in the wild.
        "private_key",
        "private-key",
        "privatekey",
        "api_key",
        "api-key",
        "apikey",
    }
)

# 2. Extensions that are almost always private-key / cert material.
_CREDENTIAL_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx"})

# 3. Config-shape extensions where the STEM signals secret content. Match
#    a credentials/secrets/api-key/private-key token at a word boundary
#    (start, end, or surrounded by `.`/`-`/`_`). A naive substring match
#    on the basename drops legitimate workspace files like
#    `skills/secretary.md`, `skills/secret-santa.md`, or
#    `credentials-helper.ts`.
_CREDENTIAL_TOKEN_RE = re.compile(
    r"(?:^|[._\-])(credentials?|secrets?|private[._\-]?keys?|api[._\-]?keys?)(?:[._\-]|$)",
    re.IGNORECASE,
)
# Includes ``.txt`` because sellers commonly paste credentials into a
# text file — ``secret.txt`` / ``credentials.txt`` / ``private-key.txt``
# would slip through a config-only extension list, and the content scan
# only inspects persona / markdown / openclaw.json.
_CREDENTIAL_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".env", ".toml", ".ini", ".cfg", ".conf", ".txt"})


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


class CredentialLeakError(Exception):
    """The synthesized openclaw-slice.json still carries a non-empty secret
    after the scrub. The slice is a SEPARATE S3 artifact the tarball safety
    scan never inspects, so this is the only gate standing between a missed
    scrub and a published credential. Raising here aborts before any artifact
    is created. The message names the leaking field PATH but never the secret.
    """


class AgentNotFoundError(Exception):
    """Seller has no agent workspace at workspace-{agent_id} on EFS."""


class InvalidAgentIdError(Exception):
    """agent_id failed format validation or path-traversal guard."""


class AgentNotInConfigError(Exception):
    """agent_id has a workspace dir on EFS but no entry in the seller's openclaw.json
    agents.list — can't synthesize a slice without the agent's identity/skills/tools.
    """


class BuiltAgentConfigError(Exception):
    """An Atlas-built staging agent dir has no parseable openclaw.json (neither
    ``workspace/openclaw.json`` nor a root-level fallback) or its config has no
    ``agents.list[].id``. Unlike the seller path, the built agent is supposed to
    be self-describing — without its own config we can't synthesize the slice
    that registers it in the owner's openclaw.json. The router maps this to 400.
    """


# Export ceiling (uncompressed). A working agent's workspace accumulates
# things that are not the agent — repo checkouts, reports, run scratch —
# and walking + tarring + scanning tens of thousands of EFS files takes
# longer than the API gateway's 30 s integration cap. Past the cap the walk
# stops and the seller is told to name what ships with ``paths``.
_MAX_EXPORT_FILES = 2000
_MAX_EXPORT_BYTES = 50 * 1024 * 1024


class ExportTooLargeError(Exception):
    """The workspace walk crossed ``_MAX_EXPORT_FILES`` / ``_MAX_EXPORT_BYTES``.
    Raised mid-walk, so ``file_count`` / ``byte_count`` / ``largest`` (heaviest
    top-level entries, bytes) describe what was seen up to that point. The
    router maps this to 413.
    """

    def __init__(self, *, file_count: int, byte_count: int, largest: list[tuple[str, int]]):
        self.file_count = file_count
        self.byte_count = byte_count
        self.largest = largest
        dirs = ", ".join(f"{name} ({size / 1048576:.1f} MB)" for name, size in largest) or "-"
        super().__init__(
            f"agent workspace is too large to export as-is: stopped after {file_count} files / "
            f"{byte_count / 1048576:.1f} MB (limit {_MAX_EXPORT_FILES} files / {_MAX_EXPORT_BYTES >> 20} MB); "
            f"heaviest entries so far: {dirs}. Pass `paths` to export only the agent's own files"
        )


class InvalidExportPathsError(Exception):
    """An explicit ``paths`` entry is absolute, traverses ``..``, resolves
    outside the agent dir, or does not exist. The router maps this to 400.
    """


class PaperclipUnavailableError(Exception):
    """The Friday-guard's Paperclip fetch FAILED (network, auth, Paperclip
    down) — distinct from Paperclip answering with a genuinely empty agent
    list. The guard must fail CLOSED on this: treating a fetch failure as
    "no agents" let a seller export Friday (owner-keyed identity/memory into
    an immutable artifact) during any Paperclip outage. The router maps this
    to a retryable 503.
    """


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase, collapse non-alphanumeric runs to single hyphens, strip
    leading/trailing hyphens — the conventional display-name → slug form.

    Legacy marketplace listings (created before agent workspaces became
    UUID-keyed) stored ``agent_id`` as a slugified display name; this mirrors
    that derivation so we can map the stored slug back to the live workspace.
    """
    return _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")


def _resolve_workspace_id(seller_id: str, agent_id: str, config: dict | None) -> str:
    """Map a stored ``agent_id`` to the live ``workspace-{id}`` dir name.

    Resolution order:
      1. ``agent_id`` is itself a live workspace dir → return as-is. Covers
         every current UUID-keyed listing — zero behavior change.
      2. Legacy slug: scan the seller's ``agents.list`` for an entry whose
         display-name (or own id) slugifies to ``agent_id`` AND whose ``id``
         is a live workspace dir; return that ``id`` (the UUID).

    Marketplace listings created before agent workspaces became UUID-keyed
    persisted ``agent_id`` as a slug (e.g. ``"vera"``) that no longer names a
    directory, so their republish/export 404s. Resolving the slug forward lets
    those listings self-heal: the export keys its slice + manifest off the
    resolved UUID, which ``replace_artifact`` then persists onto the row.

    On an ambiguous slug (>1 live agent slugifies the same way) raises
    AgentNotFoundError rather than guessing. On a clean miss, returns
    ``agent_id`` UNCHANGED so the caller's existing ``_build_tarball``
    existence check raises the familiar ``agent dir not found`` error — no
    behavior change for genuinely-missing agents.
    """
    workspace = get_workspace()
    live_dirs = set(workspace.list_workspace_agent_dirs(seller_id))

    # Path 1 — direct hit (UUIDs and any still-valid id).
    if agent_id in live_dirs:
        return agent_id

    # Path 2 — slug fallback over the seller's openclaw.json agents.list.
    candidates: set[str] = set()
    if config:
        for entry in _agents_list(config):
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id")
            if not isinstance(eid, str) or eid not in live_dirs:
                continue
            ident = entry.get("identity")
            display = entry.get("name") or (ident.get("name") if isinstance(ident, dict) else None) or ""
            if _slugify(display) == agent_id or _slugify(eid) == agent_id:
                candidates.add(eid)

    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise AgentNotFoundError(
            f"agent_id {agent_id!r} is ambiguous: {sorted(candidates)} all match; "
            "re-select the agent in the publish wizard"
        )

    # No resolution — hand back the original id so the existence check
    # downstream raises the same AgentNotFoundError it always did.
    return agent_id


def _resolve_agent_dir(seller_id: str, agent_id: str, *, config: dict | None = None) -> Path:
    """Resolve the seller's agent WORKSPACE directory on EFS.

    The OpenClaw EFS layout splits each agent across two sibling roots
    under the seller's user dir:

      agents/{id}/      — OpenClaw runtime state (sessions/ logs,
                          agent/ auth tokens). Internal; not seller content.
      workspace-{id}/  — the seller's actual workspace: persona files
                          (IDENTITY.md / SOUL.md / USER.md / HEARTBEAT.md /
                          TOOLS.md), bundled skills (skills/), MCP server
                          code, memory, etc. This is what buyers need.

    The export tars the WORKSPACE dir. Pointing at agents/{id}/ instead
    would only capture OpenClaw's internal state — the resulting tarball
    would be either runtime junk or empty after filtering. Buyers need
    workspace-{id}/; the existing filter list strips node_modules, .git,
    dotfiles, etc. so the published artifact stays clean.

    ``config`` (the seller's openclaw.json) is used to resolve a legacy slug
    ``agent_id`` forward to its live workspace UUID (see ``_resolve_workspace_id``).
    Pass it whenever available; ``None`` keeps direct-only resolution.

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

    # Resolve legacy slug → live workspace UUID before building the path. The
    # resolver only ever returns names that are members of
    # ``list_workspace_agent_dirs`` (or the original ``agent_id`` on a miss),
    # so the traversal guard below still holds on the resolved name.
    resolved_id = _resolve_workspace_id(seller_id, agent_id, config)

    candidate = (seller_root / f"workspace-{resolved_id}").resolve()

    try:
        candidate.relative_to(seller_root.resolve())
    except ValueError:
        put_metric("marketplace.path_traversal_attempt")
        raise InvalidAgentIdError(f"agent_id resolves outside the seller's user root: {agent_id!r}")

    return candidate


def resolve_workspace_agent_id(seller_id: str, agent_id: str) -> str:
    """Resolve a (possibly legacy-slug) ``agent_id`` to its live workspace dir
    name (the UUID), using the same regex + slug fallback as the export path.

    The router calls this BEFORE the Friday guard + routine capture so those run
    against the resolved agent, not a slug that would slip past them (a slug like
    ``"friday"`` resolves to the CEO's UUID, which the guard then matches). Returns
    the resolved id, or the original ``agent_id`` unchanged on a clean miss (the
    downstream export then 404s as before). Raises ``InvalidAgentIdError`` /
    ``AgentNotFoundError`` on a bad id or an ambiguous slug.
    """
    workspace = get_workspace()
    try:
        config = workspace.read_openclaw_config(seller_id)
    except WorkspaceError:
        config = None
    return _agent_id_from_dir_name(_resolve_agent_dir(seller_id=seller_id, agent_id=agent_id, config=config).name)


def _validate_export_paths(agent_dir: Path, paths: list[str]) -> list[str]:
    """Normalize explicit include paths to workspace-relative form, dropping
    entries already covered by a listed ancestor (they would otherwise be
    tarred twice). Raises ``InvalidExportPathsError`` naming every offender.
    """
    root = agent_dir.resolve()
    clean: set[str] = set()
    bad: list[str] = []
    for raw in paths:
        rel = PurePosixPath(raw.strip())
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            bad.append(raw)
            continue
        target = agent_dir / rel
        try:
            target.resolve().relative_to(root)
        except ValueError:
            bad.append(raw)
            continue
        if not target.exists():
            bad.append(raw)
            continue
        clean.add(str(rel))
    if bad:
        raise InvalidExportPathsError(
            "invalid export paths (must exist and be workspace-relative, no '..'): " + ", ".join(repr(b) for b in bad)
        )
    kept: list[str] = []
    for p in sorted(clean):
        if not any(p.startswith(k + "/") for k in kept):
            kept.append(p)
    return kept


def _build_tarball(agent_dir: Path, *, paths: list[str] | None = None) -> tuple[bytes, list[str], list[str]]:
    """Tar (gzip) an agent directory, skipping junk dirs and dotfiles at the
    top level. File mtimes are zeroed so two snapshots of unchanged content
    produce identical bytes (deterministic SHA-256).

    ``paths`` (workspace-relative files or dirs) restricts the export to
    exactly those entries; ``None`` walks the whole dir, pruning nested repo
    checkouts. Returns ``(gzip_bytes, shipped_files, pruned_dirs)``.
    """
    if not agent_dir.exists() or not agent_dir.is_dir():
        raise AgentNotFoundError(f"agent dir not found: {agent_dir}")
    include = _validate_export_paths(agent_dir, paths) if paths is not None else None

    contents: list[str] = []
    pruned: list[str] = []
    seen_files = 0
    seen_bytes = 0
    bytes_per_top: dict[str, int] = {}
    # Build the tar UNCOMPRESSED first so we control the gzip wrapper
    # ourselves. ``tarfile.open(mode="w:gz")`` delegates to
    # ``GzipFile`` with no way to pass ``mtime=0`` through, so its
    # header carries the current epoch — that one floating byte makes
    # the export non-deterministic and breaks
    # ``test_export_snapshot_is_deterministic`` whenever the two
    # consecutive exports straddle a one-second boundary.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:

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
            # A nested ``.git`` marks a repo the agent cloned to work on (the
            # company's codebase, a dependency) — not the agent. Prune the
            # whole subtree: ``.git`` itself is already skipped above, it's
            # the working tree that leaks. The agent dir's own ``.git`` (a
            # seller versioning their agent) doesn't prune the root.
            if tarinfo.isdir() and include is None and len(parts) > 1:
                if os.path.lexists(os.path.join(agent_dir, tarinfo.name, ".git")):
                    pruned.append("/".join(parts[1:]))
                    return None
            # Filename-based credential-leak guard. The seller may have
            # plopped a credentials JSON (resy-credentials.json,
            # aws.credentials.json, *.secret.json, id_rsa, etc.) into their
            # workspace. marketplace_safety.scan also greps content for
            # credential patterns, but a filename-level skip is cheap
            # defense in depth — if the file matches a credential shape,
            # we never tar it in the first place.
            basename = parts[-1]
            if basename in _SKIP_FILE_BASENAMES:
                return None
            # Content check, not name check: package.json is a legitimate
            # workspace file unless it declares openclaw.extensions (which
            # registers a workspace plugin in the buyer's container).
            # Unreadable files fail closed like unparseable ones.
            if basename == "package.json":
                try:
                    raw = (agent_dir / tarinfo.name).read_bytes()
                except OSError:
                    return None
                if package_json_declares_openclaw_extensions(raw):
                    return None
            # Skip editing backups / temp leftovers. Any name containing ".bak"
            # is a backup (.bak, .bak.<ts>, .bak-<ts>); plus .tmp/.orig/editor ~
            # files. These ride along from in-place edits of workspace files
            # (e.g. BOOTSTRAP.md.bak.20260624050826, openclaw.json.bak-20260620-…)
            # and are inert cruft that should never reach a buyer's workspace.
            if ".bak" in basename or basename.endswith((".tmp", ".orig", "~")):
                return None
            # build-manifest.json is an Atlas-build artifact (file hashes + build
            # metadata), never a real workspace file — exclude it so it can't ride
            # into a deployed agent on the staging-export fallback path (where the
            # staging root, not workspace/, is tarred).
            if basename == "build-manifest.json":
                return None
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
                nonlocal seen_files, seen_bytes
                seen_files += 1
                seen_bytes += tarinfo.size
                bytes_per_top[parts[1]] = bytes_per_top.get(parts[1], 0) + tarinfo.size
                if seen_files > _MAX_EXPORT_FILES or seen_bytes > _MAX_EXPORT_BYTES:
                    raise ExportTooLargeError(
                        file_count=seen_files,
                        byte_count=seen_bytes,
                        largest=sorted(bytes_per_top.items(), key=lambda kv: -kv[1])[:3],
                    )
                contents.append(tarinfo.name)
            return tarinfo

        # arcname="." gives us paths like "./openclaw.json" — same convention
        # as catalog_package.tar_directory uses for catalog uploads.
        if include is None:
            tf.add(str(agent_dir), arcname=".", filter=_filter)
        else:
            for rel in include:
                tf.add(str(agent_dir / rel), arcname=f"./{rel}", filter=_filter)

    # Gzip the tar with mtime=0 so two consecutive exports of unchanged
    # content produce byte-identical artifacts (load-bearing for the
    # listing version's manifest_sha256 dedupe + deterministic deploys).
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(tar_buf.getvalue())

    return gz_buf.getvalue(), sorted(contents), sorted(pruned)


def _summarize_openclaw_config(agent_dir: Path) -> dict:
    """Extract a small summary from openclaw.json if present.

    Best-effort; returns minimal dict on missing / invalid file.
    """
    cfg_path = agent_dir / "openclaw.json"
    if not cfg_path.exists():
        return {"name": _agent_id_from_dir_name(agent_dir.name), "description": ""}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": _agent_id_from_dir_name(agent_dir.name), "description": ""}

    identity = data.get("identity", {}) if isinstance(data, dict) else {}
    return {
        "name": identity.get("name") or _agent_id_from_dir_name(agent_dir.name),
        "description": identity.get("description") or identity.get("vibe") or "",
    }


def _scan_tarball_for_safety_flags(tarball_bytes: bytes, *, include_snippets: bool = False) -> list[dict]:
    """Walk the snapshot tarball, partition files by safety-scan format
    (persona / openclaw / skillmd), run each bucket, return the union as
    JSON-shaped dicts ready for the manifest.

    Trust-seller posture: this surfaces findings inline in the wizard so
    the seller can review + scrub before submit. Nothing here redacts.
    Non-text / binary files are skipped (the scanner's binary heuristic
    + size cap handles them).

    ``snippet`` — the raw matching source line — is STRIPPED by default:
    everything this feeds except the preview ends up in a manifest, and
    manifests are served by the unauthenticated listing-detail + search
    routes (see ``marketplace_safety.flags_without_snippets``). Only the
    authenticated seller-preview opts back in.
    """
    persona_files: dict[str, bytes] = {}
    openclaw_files: dict[str, bytes] = {}
    skillmd_files: dict[str, bytes] = {}

    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            basename = name.rsplit("/", 1)[-1]
            if basename in _PERSONA_FILES:
                bucket = persona_files
            elif basename == "openclaw.json":
                bucket = openclaw_files
            elif basename.endswith(".md"):
                bucket = skillmd_files
            elif member.size <= _SCAN_MAX_FILE_BYTES:
                # Every OTHER text-like file: a credential or `curl | bash` in
                # a .py/.sh/.env ships just as readily as in openclaw.json, so
                # scan it with the code-format patterns. scan()'s binary sniff
                # skips non-text; the size cap keeps big assets out.
                bucket = openclaw_files
            else:
                continue
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            bucket[name] = fobj.read()

    flags: list[marketplace_safety.SafetyFlag] = []
    if persona_files:
        flags.extend(marketplace_safety.scan(persona_files, format="persona"))
    if openclaw_files:
        flags.extend(marketplace_safety.scan(openclaw_files, format="openclaw"))
    if skillmd_files:
        flags.extend(marketplace_safety.scan(skillmd_files, format="skillmd"))

    dicts = [dataclasses.asdict(f) for f in flags]
    return dicts if include_snippets else marketplace_safety.flags_without_snippets(dicts)


def export_agent_from_efs(
    seller_id: str,
    agent_id: str,
    *,
    selected_mcps: list[str] | None = None,
    selected_skills: list[str] | None = None,
    enable_acp: bool = False,
    requires_desktop: bool = False,
    paths: list[str] | None = None,
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
        enable_acp: Seller's declaration that the agent delegates to ACP
            coding harnesses. Recorded as ``manifest["requires_acp"]``; the
            deploy side uses it to enable the buyer container's ACP runtime.
        requires_desktop: Seller's declaration that the agent needs the Isol8
            desktop app (its local node-host — browser automation, local exec
            via the operator relay). We can't scan for this — it's a runtime
            capability, not a tool name — so the publisher sets it. Recorded as
            ``manifest["requires_desktop"]`` for buyer-facing disclosure only
            (a web-only buyer must know the agent won't fully run without the
            desktop app). Nothing on the deploy side can "install" it.
        paths: Workspace-relative files/dirs to ship instead of the whole
            workspace (see ``_build_tarball``).

    Raises:
        InvalidAgentIdError: agent_id failed format / path-traversal check.
        AgentNotFoundError: agent_id not found on EFS for this seller.
        AgentNotInConfigError: agent dir exists but agent_id isn't registered
            in the seller's openclaw.json (or the config file is missing).
        InvalidExportPathsError: a ``paths`` entry is invalid or missing.
        ExportTooLargeError: the walk crossed the export cap.
    """
    workspace = get_workspace()
    try:
        config = workspace.read_openclaw_config(seller_id)
    except WorkspaceError:
        # Bad seller_id — let _resolve_agent_dir surface it as InvalidAgentIdError.
        config = None
    agent_dir = _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id, config=config)
    # The dir name is ``workspace-{uuid}`` — strip the layout prefix to get
    # the live workspace UUID. For legacy slug-keyed listings this differs
    # from the requested agent_id — key the slice, MCP-arg rewrite, cron
    # filter, and manifest off the resolved UUID so the snapshot self-heals
    # (replace_artifact then persists the UUID onto the row).
    resolved_agent_id = _agent_id_from_dir_name(agent_dir.name)
    tarball_bytes, contents, pruned = _build_tarball(agent_dir, paths=paths)

    if not config:
        raise AgentNotInConfigError(
            f"seller {seller_id!r} has no openclaw.json — publish the agent from a container that has been provisioned"
        )
    workspace_skill_names = _enumerate_workspace_skill_names(agent_dir)
    try:
        slice_ = extract_agent_slice(
            config,
            resolved_agent_id,
            selected_mcps=selected_mcps,
            selected_skills=selected_skills,
            workspace_skill_names=workspace_skill_names,
        )
    except KeyError as exc:
        raise AgentNotInConfigError(
            f"agent {agent_id!r} is not registered in seller's openclaw.json agents.list"
        ) from exc
    _rewrite_mcp_args_paths(slice_, seller_agent_id=resolved_agent_id)
    slice_["cron_jobs"] = filter_cron_jobs_for_agent(workspace.read_cron_jobs(seller_id), resolved_agent_id)

    # Post-scrub credential-leak gate. The slice is a separate S3 artifact
    # the tarball safety scan never walks, so re-inspect it here: if any
    # secret-shaped field survived the scrub (the scrub is shallow one-level
    # — a nested secret slips past) or an OAuth client-secret literal rode
    # through, BLOCK the publish before the artifact exists. The error names
    # the leaking field paths but never the secret value.
    slice_leak_flags = marketplace_safety.assert_slice_scrubbed(slice_)
    if marketplace_safety.has_blocking_credential_findings(slice_leak_flags):
        paths = ", ".join(f.snippet.split(" = ", 1)[0] for f in slice_leak_flags)
        raise CredentialLeakError(
            f"cannot publish: a credential survived scrubbing in the published slice ({paths}); "
            "remove the secret from the agent's MCP/skill config and retry"
        )

    safety_flags = _scan_tarball_for_safety_flags(tarball_bytes)

    summary = _summarize_openclaw_config(agent_dir)
    manifest = {
        "name": summary["name"],
        "description": summary["description"],
        "format": "openclaw",
        "exported_at": int(time.time()),
        "agent_id": resolved_agent_id,
        "file_count": len(contents),
        "safety_flags": safety_flags,
        # Seller's per-agent ACP declaration (see ArtifactFromAgentRequest).
        # The deploy side reads this to decide whether to turn on the buyer
        # container's ACP coding-harness runtime. Always written as a bool so
        # the deploy gate is a simple ``manifest.get("requires_acp")`` truthy
        # check; pre-flag listings simply lack the key (→ falsy).
        "requires_acp": bool(enable_acp),
        # Publisher-declared "needs the desktop app" flag — disclosure only (we
        # can't scan or provision it). pre-flag listings lack the key (→ falsy).
        "requires_desktop": bool(requires_desktop),
    }

    # Scan the agent for what it actually needs (tools, skills, external APIs,
    # credentials) so the manifest discloses requirements accurately. The
    # effective config is the per-workspace openclaw.json when present (what the
    # agent runs from), else the seller's global config.
    effective_cfg = load_effective_config(agent_dir, config)
    reqs, scan_incomplete = requirements_scan(
        effective_cfg,
        agent_dir,
        bundled_mcps=set(selected_mcps or []),
        bundled_skills=set(selected_skills or []),
        # Scan EXACTLY the files that ship (the tarball contents) so the ledger
        # never discloses a dep from a file the export filter dropped.
        scan_files=contents,
        workspace_skills=workspace_skill_names,
        declared_skills=_agent_declared_skills(config, resolved_agent_id),
    )
    manifest.update(build_manifest_requirements(reqs, scan_incomplete))

    return CatalogPackage(
        format="openclaw",
        manifest=manifest,
        openclaw_slice=slice_,
        tarball_bytes=tarball_bytes,
        tarball_contents=contents,
        pruned_paths=pruned,
    )


# ---------------------------------------------------------------------------
# Atlas agent-builder: snapshot a freshly-built STAGING agent
# ---------------------------------------------------------------------------


def _read_staging_openclaw_config(staging_dir: Path) -> dict | None:
    """Read an Atlas-built agent's OWN openclaw.json out of its staging dir.

    The skeleton-generator nests the agent's config (and persona files) under
    a ``workspace/`` subdir — see atlas-skeleton-generator.js ``draftToFastIo``
    (every generated artifact is keyed ``workspace/...``). We read
    ``workspace/openclaw.json`` first, then fall back to a root-level
    ``openclaw.json`` defensively. Returns the parsed dict, or ``None`` when
    neither candidate exists / parses.
    """
    for candidate in (staging_dir / "workspace" / "openclaw.json", staging_dir / "openclaw.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
    return None


def export_built_agent_from_staging(
    owner_id: str,
    staging_agent_id: str,
    *,
    selected_mcps: list[str] | None = None,
    selected_skills: list[str] | None = None,
) -> CatalogPackage:
    """Snapshot an Atlas-built STAGING agent dir on EFS into a CatalogPackage.

    Atlas (the in-container recruiter) builds a new specialist agent's files
    into a staging workspace dir, then asks the backend to register it. Unlike
    ``export_agent_from_efs`` — which sources the slice from the OWNER's root
    openclaw.json and 400s if the agent isn't listed there — the staging agent
    is **self-describing**: the skeleton-generator writes the agent's own
    ``openclaw.json`` + persona files under a ``workspace/`` subdir of the
    staging dir, plus a ``build-manifest.json`` at the staging-dir root. So we
    tar the inner ``workspace/`` (files land at the tarball root —
    ``./openclaw.json``, ``./AGENTS.md``, … — the convention
    ``deploy_from_artifact`` expects) and build the slice from the staging
    dir's OWN config.

    ``build-manifest.json`` sits OUTSIDE ``workspace/`` so it never reaches the
    shipped tarball (it documents the build, not the runtime workspace).

    **The build never supplies an agent id.** ``staging_agent_id`` is a
    build-scoped staging TOKEN (``atlas-staging-{build_id}``, minted by
    ``atlas-skeleton-generator.js``) that names a directory, not an agent. Any
    ``id`` in the staged ``agents.list[0]`` is normalized to that token before
    slicing, and the real id is assigned downstream by the Paperclip mint. So a
    build that writes a display-name slug into its own config cannot make that
    slug an agent id — it can only name its own staging dir contents.

    Args:
        owner_id: Clerk/org id that owns the EFS dir (same role ``seller_id``
            plays in ``export_agent_from_efs``).
        staging_agent_id: The ``workspace-{token}/`` dir name Atlas built into.
        selected_mcps / selected_skills: forwarded to extract_agent_slice; None (Atlas) keeps the {agent, plugins, tools} shape.

    Raises:
        InvalidAgentIdError: staging_agent_id failed format / path-traversal check.
        AgentNotFoundError: no staging dir at workspace-{staging_agent_id}.
        BuiltAgentConfigError: the staging dir has no parseable openclaw.json
            (or its config carries no ``agents.list[]`` entry).
        CredentialLeakError: a credential survived scrubbing in the built slice.
    """
    # Resolve the staging workspace dir on EFS. owner_id is the EFS dir owner,
    # exactly as seller_id is for export_agent_from_efs. Direct-hit resolution
    # only (config=None) — the staging dir name IS a live workspace dir, so the
    # legacy slug-forwarding the seller path needs doesn't apply here.
    staging_dir = _resolve_agent_dir(seller_id=owner_id, agent_id=staging_agent_id, config=None)

    # The shippable files live under workspace/ (see atlas-skeleton-generator.js
    # draftToFastIo). Tar that subdir so files land at the tarball root; this
    # also leaves build-manifest.json (staging root) out of the tarball. Fall
    # back to the staging root if no workspace/ subdir exists (defensive).
    workspace_subdir = staging_dir / "workspace"
    tar_root = workspace_subdir if workspace_subdir.is_dir() else staging_dir
    tarball_bytes, contents, _ = _build_tarball(tar_root)

    # Build the slice from the staging dir's OWN openclaw.json (not the owner's
    # root config) — agents.list[0] is the built agent. Same extract_agent_slice
    # logic as the seller path, just a different config source.
    staging_config = _read_staging_openclaw_config(staging_dir)
    if not staging_config:
        raise BuiltAgentConfigError(
            f"staging agent {staging_agent_id!r} has no parseable openclaw.json — "
            "the build did not write its config (expected workspace/openclaw.json)"
        )
    agents = [a for a in _agents_list(staging_config) if isinstance(a, dict)]
    if not agents:
        raise BuiltAgentConfigError(f"staging agent {staging_agent_id!r} openclaw.json has no agents.list[] entry")
    if len(agents) > 1:
        # The skeleton generator always emits a single-agent config. More than
        # one means a malformed build — fail loud rather than silently snapshot
        # agents[0] and discard the rest.
        names = [a.get("name") or a.get("id") for a in agents]
        raise BuiltAgentConfigError(
            f"staging agent {staging_agent_id!r} openclaw.json lists multiple agents ({names}); "
            "a built agent must be single-agent"
        )
    agent_entry = agents[0]
    # THE BUILD DOES NOT GET TO CHOOSE AN AGENT ID. Whatever the staged entry
    # carries in ``id`` — a display-name slug, a guessed uuid, nothing at all —
    # is overwritten here with the staging handle the platform itself resolved,
    # and the real id is assigned later (the Paperclip uuid pinned by
    # ``deploy_from_artifact``, which sets ``agent_entry["id"] = new_agent_id``).
    # The generator emits this same token, so the happy path is a no-op; the
    # normalization is what makes an improvised id inert rather than adopted.
    # A ``product-analytics-scorecard``-shaped slug reaching production as a live
    # agent id is exactly the incident this closes.
    built_agent_id = staging_agent_id
    agent_entry["id"] = built_agent_id

    workspace_skill_names = _enumerate_workspace_skill_names(tar_root)
    try:
        slice_ = extract_agent_slice(
            staging_config,
            built_agent_id,
            selected_mcps=selected_mcps,
            selected_skills=selected_skills,
            workspace_skill_names=workspace_skill_names,
        )
    except KeyError as exc:
        raise BuiltAgentConfigError(
            f"built agent {built_agent_id!r} is not registered in its own openclaw.json agents.list"
        ) from exc

    # Rewrite workspace-relative MCP arg paths to the __AGENT_DIR__ sentinel that
    # deploy_from_artifact substitutes back to the new agent's workspace — same
    # as the seller path. No-op today (built agents have no MCP servers) but
    # required once the skeleton generator emits MCP config pointing at the
    # staging workspace, else those paths would survive pointing at the staging dir.
    _rewrite_mcp_args_paths(slice_, seller_agent_id=built_agent_id)

    # Same post-scrub credential-leak gate the seller path runs: the slice is a
    # separate artifact the tarball scan never walks, so block if a secret
    # survived scrubbing. Names the field path, never the secret value.
    slice_leak_flags = marketplace_safety.assert_slice_scrubbed(slice_)
    if marketplace_safety.has_blocking_credential_findings(slice_leak_flags):
        paths = ", ".join(f.snippet.split(" = ", 1)[0] for f in slice_leak_flags)
        raise CredentialLeakError(
            f"cannot register: a credential survived scrubbing in the built agent's slice ({paths}); "
            "remove the secret from the agent's MCP/skill config and retry"
        )

    safety_flags = _scan_tarball_for_safety_flags(tarball_bytes)

    # Display name comes from the built agent's own entry — ``name``, then the
    # identity block. Deliberately NOT falling back to the entry's id: that is
    # the build's staging token now, and "atlas-staging-build_042" is a worse
    # display name than none. The register router passes the caller's ``name``
    # to the Paperclip mint anyway, and ``deploy_from_artifact`` only reads this
    # manifest name as its third-choice fallback.
    identity = agent_entry.get("identity") if isinstance(agent_entry.get("identity"), dict) else {}
    name = agent_entry.get("name") or identity.get("name") or ""
    # Entry-level ``description`` is the only schema-legal home for this text
    # (IdentitySchema is .strict() with name/theme/emoji/avatar only, so the
    # identity.* fallbacks can only ever match in an unvalidated staging file).
    description = agent_entry.get("description") or identity.get("description") or identity.get("vibe") or ""

    manifest = {
        "name": name,
        "description": description,
        "format": "openclaw",
        "exported_at": int(time.time()),
        # The staging handle, not an agent id — the real one is minted after this
        # snapshot and pinned by ``deploy_from_artifact``.
        "agent_id": built_agent_id,
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


def _agent_declared_skills(config: dict, agent_id: str) -> set[str]:
    """The skills the agent declares on its roster entry — what it uses even
    when no pipeline ``--tool``s them. The slice carries this list, so a
    declared-but-unbundled skill must still surface in the requirements ledger.
    """
    entries = _agents_list(config)
    entry = next((a for a in entries if isinstance(a, dict) and a.get("id") == agent_id), {})
    return {s for s in (entry.get("skills") or []) if isinstance(s, str)}


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
    workspace = get_workspace()
    try:
        config = workspace.read_openclaw_config(seller_id) or {}
    except WorkspaceError:
        # Bad seller_id — let _resolve_agent_dir surface it as InvalidAgentIdError.
        config = {}
    agent_dir = _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id, config=config)
    # Resolved workspace UUID (dir name minus the ``workspace-`` layout
    # prefix) — differs from agent_id only for legacy slug listings.
    resolved_agent_id = _agent_id_from_dir_name(agent_dir.name)

    # Validate the agent is registered in the seller's config; the entry
    # itself is NOT used as a skills source (no per-agent skill scoping
    # exists at runtime — see the addable-skills note below).
    agents_list = _agents_list(config)
    agent_entry = next(
        (a for a in agents_list if isinstance(a, dict) and a.get("id") == resolved_agent_id),
        None,
    )
    if not isinstance(agent_entry, dict):
        raise AgentNotInConfigError(f"agent {agent_id!r} is not registered in the seller's openclaw.json roster")

    workspace_skill_names = _enumerate_workspace_skill_names(agent_dir)

    # Workspace skills auto-ship via the tarball + buyer-side discovery — they are
    # informational, NOT a selectable bundle choice.
    auto_ship_skills = sorted(workspace_skill_names)

    # Addable skills = the container's GLOBAL skills.entries the buyer would not
    # otherwise have. There is no per-agent skill scoping, so the agent's config
    # entry is NOT a reliable source; the global skills.entries are. Bundled
    # skills already ship in the buyer's image, and workspace skills auto-ship
    # above, so neither belongs in the selectable add-list.
    skills_cfg = config.get("skills") if isinstance(config.get("skills"), dict) else {}
    entries_cfg = skills_cfg.get("entries") if isinstance(skills_cfg.get("entries"), dict) else {}
    addable_skill_names = sorted(entries_cfg.keys())
    available_skills = [
        {
            "name": name,
            "bucket": classify_skill(name, config, workspace_skill_names=workspace_skill_names).value,
        }
        for name in addable_skill_names
    ]

    mcp_servers = (config.get("mcp") or {}).get("servers") or {}
    available_mcps = sorted(mcp_servers.keys()) if isinstance(mcp_servers, dict) else []

    persona_files = _enumerate_persona_files(agent_dir)

    # Build the tarball + scan for safety_flags. We don't return the
    # tarball itself — just the flags. ``contents`` (the shipped-file list)
    # scopes the requirements scan below to exactly what ships.
    tarball_bytes, contents, _ = _build_tarball(agent_dir)
    # Snippets stay ON here and ONLY here: the preview is an authenticated,
    # seller-scoped response showing the seller their OWN workspace lines so
    # they can fix a finding before submit. It is never persisted.
    safety_flags = _scan_tarball_for_safety_flags(tarball_bytes, include_snippets=True)

    # Surface (but do NOT block on) any credential that would survive the
    # scrub in the published slice. Preview is informational — the wizard's
    # Review step renders these inline so the seller fixes them BEFORE
    # submit, where export_agent_from_efs raises CredentialLeakError. Build a
    # slice over everything available so the preview flags match what the
    # actual publish would catch. Best-effort: a slice-build failure here
    # must never break the preview.
    try:
        preview_slice = extract_agent_slice(
            config,
            resolved_agent_id,
            selected_mcps=available_mcps,
            selected_skills=addable_skill_names,
            workspace_skill_names=workspace_skill_names,
        )
        slice_leak_flags = marketplace_safety.assert_slice_scrubbed(preview_slice)
        safety_flags = safety_flags + [dataclasses.asdict(f) for f in slice_leak_flags]
    except Exception as exc:  # noqa: BLE001 — preview must not raise
        logger.warning("agent_export: preview slice-leak scan failed for agent=%s: %s", agent_id, exc)

    # Live requirements scan so the wizard's Requirements step renders what the
    # agent needs. Classify optimistically — assume everything available is
    # bundled — so the wizard shows the full ledger up front (the frozen manifest
    # at submit reflects the seller's actual selection). Scoped to shipped files.
    effective_cfg = load_effective_config(agent_dir, config)
    reqs, scan_incomplete = requirements_scan(
        effective_cfg,
        agent_dir,
        bundled_mcps=set(available_mcps),
        bundled_skills=set(addable_skill_names),
        scan_files=contents,
        workspace_skills=workspace_skill_names,
        declared_skills={s for s in (agent_entry.get("skills") or []) if isinstance(s, str)},
    )

    return {
        "agent_id": resolved_agent_id,
        "available_mcps": available_mcps,
        "available_skills": available_skills,
        "auto_ship_skills": auto_ship_skills,
        "persona_files": persona_files,
        "safety_flags": safety_flags,
        "requirements": [r.to_manifest() for r in reqs],
        "scan_incomplete": scan_incomplete,
    }


# ---------------------------------------------------------------------------
# Friday-publish guard
# ---------------------------------------------------------------------------


async def _default_fetch_paperclip_agents(*, owner_id: str) -> list[dict]:
    """Production-path Paperclip agent fetch — constructs the httpx client,
    repo, and admin_client the same way ``ecs_manager`` does, then delegates
    to ``fetch_paperclip_agents_for_owner``. Raises
    ``PaperclipUnavailableError`` if anything fails (network, auth, Paperclip
    down): the Friday guard consumes this, and an empty return there means
    "allow the export" — returning ``[]`` on failure made the guard fail
    OPEN during any Paperclip outage. Callers that genuinely want graceful
    degrade (``resolve_apex_ceo_agent_id``) catch it themselves.
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
        # raise_on_unavailable: the shared fetcher's default contract swallows
        # availability failures into [] (provision's needs) — which this
        # guard would read as "empty roster, allow". The flag re-raises them
        # so they land in the except below and become the typed error.
        return await fetch_paperclip_agents_for_owner(
            owner_id=owner_id, repo=repo, admin_client=admin_client, raise_on_unavailable=True
        )
    except Exception as exc:  # noqa: BLE001 — every failure shape maps to the typed error
        logger.warning(
            "agent_export: default paperclip fetch failed for owner=%s: %s",
            owner_id,
            exc,
        )
        raise PaperclipUnavailableError(
            "could not verify the agent against Paperclip — retry the publish in a moment"
        ) from exc
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Routine capture (marketplace publish)
# ---------------------------------------------------------------------------

# Routine + trigger fields stripped at capture — instance/company/agent IDs and
# revision pointers that are meaningless on the buyer's side. Mirrors
# ``filter_cron_jobs_for_agent``'s ``_STRIPPED_CRON_KEYS`` posture: keep the
# portable shape (title/description/cron/timezone), drop everything the deploy
# side must regenerate or remap.
_ROUTINE_PORTABLE_KEYS = ("title", "description")
_TRIGGER_PORTABLE_KEYS = ("cronExpression", "timezone")


def _is_publishable(text: str) -> bool:
    """Whether a seller-authored routine string may go in a public manifest.

    ``manifest.json`` is served by ``GET /marketplace/listings/{id}``, and a
    routine's title and description are the seller-authored strings that reach
    it WITHOUT passing the tarball's safety scan — routine capture is appended
    after ``export_agent_from_efs`` has already produced ``safety_flags``, so a
    finding here would not even reach moderation. They get the same patterns
    the tarball does rather than a second set of regexes.

    ANY flag fails it, not just the PII ones: the persona set also carries
    ``hardcoded-secret``, ``aws-secret-key`` and ``provider-token``, and a
    credential in a public manifest is worse than a lost prompt. The seed's
    own google descriptions trip ``email-address`` (they interpolate the
    owner's account), which is the case this started from.

    This scan — not the automation classifier above it — is what actually
    fences the seller's data in. The classifier decides what SHAPE a routine
    ports as, and it reads a trigger label the owner can edit; that makes it a
    correctness/UX decision, not a security boundary. Nothing seller-authored
    reaches the manifest without passing through here first.
    """
    return not marketplace_safety.scan({"routine-text": text.encode()}, format="persona")


def _portable_routine_from_detail(detail: dict, agent_id: str) -> dict | None:
    """Strip a Paperclip RoutineDetail down to its buyer-portable shape.

    Two shapes, decided per ROUTINE:

    - A **seeded automation** — it carries a webhook trigger whose ``label``
      inverts through the registry — ports as the single requirement
      ``{title, kind: "webhook", provider, event_id}``, whatever else it
      carries. The buyer arms it themselves: the live trigger's resource
      (repo / project / Page / mailbox) is the SELLER's, and a deploy must not
      silently arm a trigger the buyer didn't ask for.

      It carries NO description and NONE of its schedules, because both are
      seller-instance state. The seed interpolates the concrete account, proxy
      URL and watch state-file path into the description, and the manifest is
      served publicly by ``GET /marketplace/listings/{id}`` — so copying it
      would publish the seller's own mailbox and endpoint. Its schedule
      triggers are the watch's twice-weekly self-renewal, which on a buyer
      would re-arm a watch that does not exist. Only the title ports, and that
      is rendered from the registry, not from the seller.

      Recognition is the trigger LABEL, because the routine's own title is
      both mutable and guessable: renaming a routine is a title-only edit the
      detail panel supports, and nothing stops a seller titling an ordinary
      cron exactly like a registry automation. A rename never reaches a
      trigger, and the user-authored webhook path ("Any other app" in
      ``RoutinesPanel``) sets no label at all — so a hand-built webhook
      routine falls through to the schedule branch and keeps its real
      scheduled work.

      A schedule-only seeded automation (the gmail poll shape) has no webhook
      trigger to recognise, so it is not a requirement. It does not become a
      broken schedule either: its whole prompt is the seller's mailbox and the
      ``gog`` invocation against it, so the scan below takes the description,
      and a routine stripped of its instructions ports as nothing at all.

    - Anything else — one ``{title, description, cron, timezone}`` per schedule
      trigger (title/description repeated per trigger so the deploy side can
      recreate one routine+trigger per entry), recreated DORMANT on the buyer.
      Both its title and its description go through ``_is_publishable`` first
      — they are the seller-authored strings that reach the public manifest
      without passing the tarball's safety scan. An unsafe description is
      blanked; an unsafe title takes the routine with it, since a title cannot
      be blanked and still name anything.

    ``None`` when the routine is archived, belongs to another agent, or has no
    portable trigger at all.

    Archived routines are skipped because ``delete_routine`` ARCHIVES rather
    than deletes (upstream has no DELETE route) — the same filter
    ``GET /teams/routines`` applies, for the same reason: an archived row is
    work the seller deleted, and a republish would ship it to every buyer.

    Company / agent / instance ids and revision fields are dropped — the buyer
    side remaps ``assigneeAgentId`` to the freshly-minted agent and Paperclip
    generates new routine/trigger ids.
    """
    if not isinstance(detail, dict):
        return None
    if detail.get("status") == "archived":
        return None
    if detail.get("assigneeAgentId") != agent_id:
        return None
    title = detail.get("title")
    if not isinstance(title, str) or not title:
        return None
    description = detail.get("description")
    triggers = detail.get("triggers")
    if not isinstance(triggers, list):
        return None

    # A seeded automation ports as a requirement and nothing else — see the
    # docstring for why its description and schedules stay behind.
    #
    # It identifies itself on its TRIGGER, not on the routine: the seed passes
    # the registry-rendered title as the webhook trigger's ``label``, and the
    # user-authored path ("Any other app") sets none. That is the only signal
    # here that is neither editable nor guessable — a routine rename is a
    # title-only edit that never reaches a trigger, and a seller titling an
    # ordinary cron "GitHub: Issue opened or changed" does not acquire a
    # labelled webhook trigger by doing so.
    #
    # The label is also what the requirement is NAMED by. It came from the
    # registry, so unlike the routine's own title it cannot carry seller text
    # into a public manifest.
    for trig in triggers:
        if not isinstance(trig, dict) or trig.get("kind") != "webhook":
            continue
        label = trig.get("label")
        automation = automation_from_title(label) if isinstance(label, str) else None
        if automation is None:
            # A webhook the owner built themselves. It has no requirement to
            # express, but it may carry real scheduled work — fall through.
            continue
        provider, event_id = automation
        return {"_entries": [{"title": label, "kind": "webhook", "provider": provider, "event_id": event_id}]}

    # An ordinary routine publishes its own title, and that is seller-authored
    # text like the description is. Unlike the description it cannot simply be
    # blanked — it IS the routine's identity on the buyer's board — so a title
    # that fails the scan takes the routine with it. (A seeded automation is
    # already gone by here, named by its registry label, so a seller renaming
    # one after their own mailbox costs them nothing.)
    if not _is_publishable(title):
        return None

    had_description = isinstance(description, str) and bool(description)
    safe_description = description if had_description and _is_publishable(description) else ""

    # A routine whose instructions we had to remove does not port at all. The
    # seeded gmail poll is the case that matters: schedule-only, so it never
    # becomes a requirement, and its whole prompt is the seller's mailbox and
    # the gog invocation against it. Recreating that as a titled cron with an
    # empty body hands the buyer something that looks like work and is not.
    # A routine the seller simply never described is untouched — nothing was
    # taken from it.
    if had_description and not safe_description:
        return None

    # Variable definitions ride with every schedule entry: the deploy side
    # recreates the routine from the description, and Paperclip re-syncs any
    # {{NAME}} the description references into a REQUIRED definition with no
    # default — which then 422s dormant schedule-trigger creation and rolls
    # back the whole deploy. Defaults are seller-authored text reaching the
    # public manifest, so they go through the same scan as the description;
    # a variable whose default cannot ship takes the routine with it (porting
    # the variable without its default would just recreate the 422 on the
    # buyer, and porting the routine without the variable ships a contract
    # that references an input the buyer does not have).
    raw_variables = detail.get("variables")
    variables: list[dict] = []
    if isinstance(raw_variables, list):
        for var in raw_variables:
            if not isinstance(var, dict) or not isinstance(var.get("name"), str):
                continue
            portable_var = {
                key: var[key]
                for key in ("name", "label", "type", "defaultValue", "required", "options")
                if key in var and var[key] is not None
            }
            default = portable_var.get("defaultValue")
            if isinstance(default, str) and default and not _is_publishable(default):
                return None
            variables.append(portable_var)

    out: list[dict] = []
    for trig in triggers:
        if not isinstance(trig, dict):
            continue
        if trig.get("kind") != "schedule":
            continue
        cron = trig.get("cronExpression")
        if not isinstance(cron, str) or not cron:
            continue
        entry = {
            "title": title,
            "description": safe_description,
            "cron": cron,
            "timezone": trig.get("timezone") if isinstance(trig.get("timezone"), str) else "UTC",
        }
        if variables:
            entry["variables"] = variables
        out.append(entry)
    # The caller flattens; return the first as the representative shape only if
    # there is exactly one — but routines can carry multiple schedule triggers,
    # so we hand back the full list via a thin wrapper key.
    if not out:
        return None
    return {"_entries": out}


async def capture_agent_routines(
    *,
    owner_id: str,
    agent_id: str,
    fetch_routine_details: Callable[..., Awaitable[list[dict]]] | None = None,
) -> list[dict]:
    """Capture an agent's Paperclip routines into a buyer-portable list.

    Entries are either a schedule (``{title, description, cron, timezone}``) or
    an automation requirement (``{title, kind: "webhook", provider, event_id}``
    — no description, it is seller-instance state) — see
    ``_portable_routine_from_detail`` for why a seeded automation ships as a
    requirement and never as a live trigger. All company/agent/instance ids and
    revision fields are stripped (mirrors ``filter_cron_jobs_for_agent``). Both
    the cron and the trigger kind live on the routine's *triggers*, so we read
    each routine's detail (which carries them) to pull them.

    Best-effort: any failure (Paperclip down, no company, malformed shape) logs
    and returns ``[]`` so routine capture never fails the export. The fetcher is
    injected for tests; production callers omit it and the default wires the
    admin client + repo the same way ``_default_fetch_paperclip_agents`` does.
    """
    fetch = fetch_routine_details or _default_fetch_routine_details
    try:
        details = await fetch(owner_id=owner_id, agent_id=agent_id)
    except Exception as exc:  # noqa: BLE001 — capture must never break the export
        logger.warning(
            "agent_export: routine capture failed for owner=%s agent=%s: %s",
            owner_id,
            agent_id,
            exc,
        )
        return []

    captured: list[dict] = []
    for detail in details:
        portable = _portable_routine_from_detail(detail, agent_id)
        if portable is None:
            continue
        captured.extend(portable["_entries"])
    return captured


async def _default_fetch_routine_details(*, owner_id: str, agent_id: str) -> list[dict]:
    """Production-path fetch: list the owner's company routines, filter to the
    agent, and pull each one's RoutineDetail (which carries its triggers).

    Returns ``[]`` if anything fails (network, auth, no company) so the caller
    degrades gracefully — same posture as ``_default_fetch_paperclip_agents``.
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id is None:
            return []
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        listed = await admin.list_routines(session_cookie=cookie, company_id=company_id)
        if isinstance(listed, dict):
            items = listed.get("routines") or listed.get("items") or []
        elif isinstance(listed, list):
            items = listed
        else:
            items = []
        details: list[dict] = []
        for r in items:
            if not isinstance(r, dict) or r.get("assigneeAgentId") != agent_id:
                continue
            # Archived == the seller deleted it (upstream archives instead of
            # deleting). Skipped here as well as in the portable strip, so a
            # board full of archived rows doesn't cost a detail fetch each.
            if r.get("status") == "archived":
                continue
            rid = r.get("id")
            if not isinstance(rid, str):
                continue
            detail = await admin.get_routine(session_cookie=cookie, routine_id=rid)
            if isinstance(detail, dict):
                details.append(detail)
        return details
    except Exception as exc:  # noqa: BLE001 — graceful degrade per docstring
        logger.warning(
            "agent_export: default routine-detail fetch failed for owner=%s agent=%s: %s",
            owner_id,
            agent_id,
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
      - Paperclip GENUINELY returns no agents (provisioning hasn't run).
      - No entry in the list carries ``role == "ceo"``.
      - The CEO entry's id doesn't match ``agent_id``.

    A fetch FAILURE is different: the default fetcher raises
    ``PaperclipUnavailableError``, which propagates so the router blocks the
    export with a retryable 503 — failing open here let Friday be exported
    during a Paperclip outage.

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


async def resolve_apex_ceo_agent_id(
    *,
    owner_id: str,
    fetch_paperclip_agents: Callable[..., Awaitable[list[dict]]] | None = None,
) -> str | None:
    """The owner's apex CEO (Friday) Paperclip agent id, or ``None``.

    Used by the agent-builder bridge to default ``reports_to`` so Atlas-built
    hires land under Friday in the org chart instead of unparented. Delegates
    apex selection to ``company_deploy_service._find_friday_id`` (rootless
    ``role == "ceo"``), which is safe when marketplace company deploys have
    added division-head CEOs. Graceful-degrade: any fetch failure yields
    ``None`` (unlike the Friday guard, which fails closed).
    """
    from core.services.company_deploy_service import _find_friday_id

    fetch = fetch_paperclip_agents or _default_fetch_paperclip_agents
    try:
        agents = await fetch(owner_id=owner_id)
    except PaperclipUnavailableError:
        # Unlike the Friday guard, this is a nice-to-have default (org-chart
        # placement) — degrade to unparented rather than failing the build.
        return None
    return _find_friday_id(agents)
