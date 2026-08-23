"""Normalize an uploaded OpenClaw setup into the self-describing staging layout
``agent_export.export_built_agent_from_staging`` consumes.

Accepted archive shapes (detected by marker, in this order):

  backup     — ``openclaw backup create`` output: one top-level dir holding
               ``manifest.json`` ({schemaVersion, archiveRoot, createdAt,
               assets:[{kind: state|config|credentials|workspace, sourcePath,
               archivePath}]}) and ``payload/`` mirroring absolute source paths
               (``payload/posix/<abs path>``).
  state-dir  — a plain tar of ``~/.openclaw`` (``openclaw.json`` beside
               ``agents/`` / ``workspace/``), at the root or one level down.
               Also matches an Isol8 artifact re-upload (``./openclaw.json``
               next to persona files) — then the dir IS the workspace.
  workspace  — a bare agent workspace (``AGENTS.md`` / ``SOUL.md`` /
               ``IDENTITY.md``), at the root or one level down. No config.

Extraction is TWO streaming passes over the upload (S3 object or bytes): pass
1 reads names only and extracts ``*/manifest.json`` to learn where the state
dir(s) are; pass 2 extracts with state-relative skip rules so a 300 MB archive
(Chrome profile, media, caches, global SQLite) costs ~1 MB on EFS. Everything
under ``agents/<id>/`` is dropped EXCEPT ``agent/openclaw-agent.sqlite`` and
``sessions/`` — the two history inputs (see openclaw_backup_history.py).

A candidate prefix found via a bare ``openclaw.json`` marker (state-dir shape)
only counts as a STATE dir — and gets the runtime skip rules applied under
it — when pass 1 also saw a sibling entry there named ``agents``, ``state``,
``credentials``, ``browser``, or ``workspace``. A prefix with only
``openclaw.json`` + persona files (an Isol8 artifact re-upload) is
workspace-shaped: no state-relative skips apply, so e.g. its own ``memory/``
directory is agent content, not runtime junk, and survives. Manifest-declared
``state`` assets (the backup shape) always count — that comes from the
manifest itself, not this heuristic.

Staging layout written by :func:`materialize_staging`::

    workspace-{import_id}/
      raw/                 the extracted upload (never ships; removed on confirm)
      workspace/           what ships: persona files, skills/, memory/, …
        openclaw.json      ONE agents.list entry + PRE-SCRUBBED mcp/skills/plugins/tools
      cron-jobs.json       this agent's cron jobs (staging root → never ships)
      import-manifest.json source kind, candidates, selections, warnings

``_build_tarball`` tars only ``workspace/`` — that keeps ``raw/`` and the
sidecars out of the artifact. ``workspace/openclaw.json`` DOES ship (not in
``_SKIP_FILE_BASENAMES``), which is why the blocks written there go through
the same scrub helpers ``extract_agent_slice`` uses.

Deliberately NOT folded in: ``agents.defaults`` (container-level bounds such
as ``subagents`` / ``runRetries`` are Isol8's to set) and ``channels``
(per-user credentials; ``_STRIPPED_KEYS`` drops them anyway).
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from core.services.catalog_slice import (
    _scrub_plugins_block,
    _scrub_secret_fields,
    _scrub_tools_block,
    filter_cron_jobs_for_agent,
    sanitize_agent_entry_for_schema,
    strip_user_specific_fields,
)

MAX_UPLOAD_BYTES = 2 * 1024**3  # S3 presigned PUT; far above any real archive
MAX_EXTRACTED_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 512 * 1024**2  # a single member over this is skipped + reported
MAX_MEMBERS = 200_000
MAX_METADATA_BYTES = 4 * 1024 * 1024  # manifest.json / openclaw.json / installs.json / package.json / cron jobs.json
IMPORT_ID_RE = re.compile(r"^import-[0-9a-f]{12}$")
# Agent/plugin ids are config-derived (an attacker-controlled archive's JSON),
# not archive member names — tarfile's own traversal checks (_check_member)
# don't cover them. Anything that fails this can't be used to build an
# `agents/<id>` / `workspace-<id>` / `extensions/<id>` path.
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_PERSONA_MARKERS = ("AGENTS.md", "SOUL.md", "IDENTITY.md")
_UNIVERSAL_SKIP_DIRS = frozenset({"node_modules", ".git", "__pycache__"})
_UNIVERSAL_SKIP_FILES = frozenset({".DS_Store"})
_UNIVERSAL_SKIP_SUFFIXES = (
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    "-wal",
    "-shm",
    "-journal",
    ".sock",
    ".pid",
    ".tmp",
)
# State-relative top-level dirs that are runtime, not content.
_STATE_SKIP_DIRS = frozenset(
    {
        "state",
        "memory",
        "credentials",
        "browser",
        "media",
        "completions",
        "logs",
        "delivery-queue",
        "session-delivery-queue",
        "canvas",
        "devices",
        "identity",
        "npm",
        "npm-runtime",
        "dev",
        "git",
        "tools",
        "sessions",
        "pre-migration-archive",
    }
)
# Sibling names next to a bare openclaw.json that mark its directory as an
# actual OpenClaw state dir (vs. a workspace-shaped artifact re-upload) — see
# the module docstring's "candidate prefix" note.
_STATE_MARKER_NAMES = frozenset({"agents", "state", "credentials", "browser", "workspace"})
_LOCAL_PATH_RE = re.compile(r"^(/Users/|/home/|[A-Za-z]:\\)")


class BackupImportError(ValueError):
    """The upload is not something we can import; message is user-facing."""


@dataclass
class ExtractReport:
    files: int = 0
    bytes: int = 0
    skipped_big: list[str] = field(default_factory=list)


@dataclass
class BackupSource:
    kind: str  # "backup" | "state-dir" | "workspace"
    root: Path
    state_dir: Path | None
    state_source_path: str | None
    config: dict
    workspace_dirs: list[Path] = field(default_factory=list)


@dataclass
class HistoryInfo:
    sqlite: Path | None
    sessions_dir: Path | None
    bytes: int


@dataclass
class AgentCandidate:
    id: str
    name: str
    entry: dict
    workspace_dir: Path | None
    history: HistoryInfo | None = None


@dataclass
class PluginCandidate:
    id: str
    package_name: str
    version: str
    source_dir: Path | None
    has_dependencies: bool
    config: dict


# ---------------------------------------------------------------------------
# 1. Streaming extraction
# ---------------------------------------------------------------------------


def _check_member(member: tarfile.TarInfo, dst_resolved: Path, dst: Path) -> Path:
    if member.issym() or member.islnk():
        raise BackupImportError(f"archive contains a symlink or hardlink: {member.name!r}")
    if member.name.startswith("/"):
        raise BackupImportError(f"archive member has an absolute path: {member.name!r}")
    target = (dst / member.name).resolve()
    try:
        target.relative_to(dst_resolved)
    except ValueError as exc:
        raise BackupImportError(f"archive member escapes the extraction directory: {member.name!r}") from exc
    return target


def _state_prefixes_from_manifests(dst: Path) -> list[str]:
    """After pass 1 extracted ``*/manifest.json``, return state asset archivePaths."""
    out: list[str] = []
    for mp in dst.glob("*/manifest.json"):
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for asset in manifest.get("assets") or []:
            if isinstance(asset, dict) and asset.get("kind") == "state" and isinstance(asset.get("archivePath"), str):
                out.append(asset["archivePath"].strip("/"))
    return out


def _skip_reason(name: str, state_prefixes: list[str]) -> str | None:
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return "empty"
    if any(p in _UNIVERSAL_SKIP_DIRS for p in parts[:-1]) or parts[-1] in _UNIVERSAL_SKIP_FILES:
        return "universal"
    if parts[-1].endswith(_UNIVERSAL_SKIP_SUFFIXES):
        return "universal"
    rel_parts: list[str] | None = None
    for prefix in state_prefixes:
        # R1(a): normalize every prefix (a root-level state dir has prefix
        # "", whose split yields [""] — filter that out so it becomes [],
        # matching every path instead of matching nothing.
        pp = [p for p in prefix.split("/") if p]
        if parts[: len(pp)] == pp:
            rel_parts = parts[len(pp) :]
            break
    if rel_parts is None or not rel_parts:
        return None
    top = rel_parts[0]
    if top in _STATE_SKIP_DIRS:
        return "runtime"
    if top == "cron" and len(rel_parts) > 1 and rel_parts[1] == "runs":
        return "runtime"
    if top == "agents":
        # agents/<id>/agent/openclaw-agent.sqlite and agents/<id>/sessions/** only
        if len(rel_parts) >= 4 and rel_parts[2] == "agent" and rel_parts[3] == "openclaw-agent.sqlite":
            return None
        if len(rel_parts) >= 3 and rel_parts[2] == "sessions":
            return None
        return "runtime"
    return None


def extract_archive(open_stream: Callable[[], BinaryIO], dst: Path) -> ExtractReport:
    """Two-pass streaming extraction with skip rules. ``open_stream`` must
    return a FRESH readable each call (an S3 body or a BytesIO)."""
    dst.mkdir(parents=True, exist_ok=True)
    dst_resolved = dst.resolve()
    # Candidate prefixes found via a bare `openclaw.json` marker (state-dir
    # shape), plus — for each depth-0/depth-1 prefix — the set of immediate
    # child names pass 1 observed there. Used to qualify candidates per
    # R1(b): a prefix only counts as a state dir if a marker sibling was seen.
    openclaw_prefixes: list[str] = []
    prefix_children: dict[str, set[str]] = {}
    try:
        with tarfile.open(fileobj=open_stream(), mode="r|gz") as tf:
            count = 0
            for member in tf:
                count += 1
                if count > MAX_MEMBERS:
                    raise BackupImportError(f"archive has more than {MAX_MEMBERS} entries")
                target = _check_member(member, dst_resolved, dst)
                parts = [p for p in member.name.split("/") if p not in ("", ".")]
                if len(parts) == 2 and parts[1] == "manifest.json" and member.isfile():
                    if member.size > MAX_METADATA_BYTES:
                        continue  # not a real backup manifest — never this big
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tf.extractfile(member).read())  # type: ignore[union-attr]
                elif parts and parts[-1] == "openclaw.json" and len(parts) <= 2 and member.isfile():
                    openclaw_prefixes.append("/".join(parts[:-1]))
                if parts:
                    prefix_children.setdefault("", set()).add(parts[0])
                if len(parts) >= 2:
                    prefix_children.setdefault(parts[0], set()).add(parts[1])
    except tarfile.TarError as exc:
        raise BackupImportError("not a valid .tar.gz archive") from exc
    qualified = {p for p in openclaw_prefixes if prefix_children.get(p, set()) & _STATE_MARKER_NAMES}
    state_prefixes = sorted(qualified | set(_state_prefixes_from_manifests(dst)))

    report = ExtractReport()
    try:
        with tarfile.open(fileobj=open_stream(), mode="r|gz") as tf:
            count = 0
            for member in tf:
                count += 1
                if count > MAX_MEMBERS:
                    raise BackupImportError(f"archive has more than {MAX_MEMBERS} entries")
                target = _check_member(member, dst_resolved, dst)
                if _skip_reason(member.name, state_prefixes) is not None:
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                if member.size > MAX_FILE_BYTES:
                    report.skipped_big.append(member.name)
                    continue
                report.bytes += member.size
                if report.bytes > MAX_EXTRACTED_BYTES:
                    raise BackupImportError("archive expands past the extraction cap")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                report.files += 1
    except tarfile.TarError as exc:
        raise BackupImportError("not a valid .tar.gz archive") from exc
    return report


# ---------------------------------------------------------------------------
# 2. Source detection
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise BackupImportError(f"{path.name} is too large to read")
    except OSError as exc:
        raise BackupImportError(f"{path.name} is not valid JSON") from exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupImportError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise BackupImportError(f"{path.name} must be a JSON object")
    return data


def _conventional_workspaces(state_dir: Path) -> list[Path]:
    out: list[Path] = []
    if (state_dir / "workspace").is_dir():
        out.append(state_dir / "workspace")
    out.extend(sorted(p for p in state_dir.glob("workspace-*") if p.is_dir()))
    return out


def _has_persona(d: Path) -> bool:
    return any((d / f).is_file() for f in _PERSONA_MARKERS)


def _locate_backup(root: Path, manifest_path: Path) -> BackupSource:
    manifest = _read_json(manifest_path)
    archive_root = manifest_path.parent.name
    declared = manifest.get("archiveRoot")
    if isinstance(declared, str) and declared and declared != archive_root:
        raise BackupImportError("backup manifest archiveRoot does not match the archive layout")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise BackupImportError("backup manifest has no assets")
    root_resolved = root.resolve()
    state_dir: Path | None = None
    state_source: str | None = None
    config_path: Path | None = None
    workspaces: list[Path] = []
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("archivePath"), str):
            continue
        candidate = root / asset["archivePath"]
        try:
            candidate.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise BackupImportError("backup manifest asset escapes the archive") from exc
        if not candidate.exists():
            continue
        kind = asset.get("kind")
        if kind == "state":
            state_dir = candidate
            state_source = asset.get("sourcePath") if isinstance(asset.get("sourcePath"), str) else None
        elif kind == "config":
            config_path = candidate
        elif kind == "workspace" and candidate.is_dir():
            workspaces.append(candidate)
    if config_path is None and state_dir is not None and (state_dir / "openclaw.json").is_file():
        config_path = state_dir / "openclaw.json"
    config = _read_json(config_path) if config_path is not None else {}
    if state_dir is not None:
        workspaces.extend(w for w in _conventional_workspaces(state_dir) if w not in workspaces)
    if state_dir is None and not workspaces and not config:
        raise BackupImportError("backup contains no state directory, config, or workspace")
    return BackupSource(
        kind="backup",
        root=root,
        state_dir=state_dir,
        state_source_path=state_source,
        config=config,
        workspace_dirs=workspaces,
    )


def locate_source(root: Path) -> BackupSource:
    manifests = [p for p in root.glob("*/manifest.json") if p.is_file()]
    if len(manifests) > 1:
        raise BackupImportError("archive holds more than one backup root")
    if len(manifests) == 1:
        return _locate_backup(root, manifests[0])
    for cfg in [root / "openclaw.json", *sorted(root.glob("*/openclaw.json"))]:
        if cfg.is_file():
            state_dir = cfg.parent
            return BackupSource(
                kind="state-dir",
                root=root,
                state_dir=state_dir,
                state_source_path=None,
                config=_read_json(cfg),
                workspace_dirs=_conventional_workspaces(state_dir),
            )
    for ws in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
        if _has_persona(ws):
            return BackupSource(
                kind="workspace", root=root, state_dir=None, state_source_path=None, config={}, workspace_dirs=[ws]
            )
    raise BackupImportError("no OpenClaw backup, state directory, or agent workspace found in the archive")


# ---------------------------------------------------------------------------
# 3. Enumeration
# ---------------------------------------------------------------------------


def _identity_md_field(ws: Path | None, label: str) -> str | None:
    if ws is None:
        return None
    path = ws / "IDENTITY.md"
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^\s*[-*]?\s*\**{re.escape(label)}\**\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            value = m.group(1).strip().strip("*").strip()
            if value and not value.startswith("("):
                return value
    return None


def _contained(source: BackupSource, candidate: Path) -> Path | None:
    """Reject a config-derived candidate that resolves outside the extracted
    archive root — ``state_dir`` is always under ``source.root``, so one
    containment root suffices for every branch below. Config values like
    ``agents.list[].workspace`` are attacker-controlled JSON from the upload,
    not archive member names, so tarfile's own traversal checks don't cover
    them; without this a ``workspace: "~/.openclaw/../../../etc"`` entry
    would resolve to a real directory outside the staging sandbox and get
    copied into the buyer-visible workspace."""
    try:
        candidate.resolve().relative_to(source.root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_config_path(source: BackupSource, raw: str) -> Path | None:
    norm = raw.replace("\\", "/").rstrip("/")
    if source.state_dir is not None:
        if norm == "~/.openclaw" or norm.startswith("~/.openclaw/"):
            rest = norm[len("~/.openclaw") :].lstrip("/")
            candidate = source.state_dir / rest if rest else source.state_dir
            return _contained(source, candidate)
        if source.state_source_path:
            st = source.state_source_path.replace("\\", "/").rstrip("/")
            if norm == st or norm.startswith(st + "/"):
                rest = norm[len(st) :].lstrip("/")
                candidate = source.state_dir / rest if rest else source.state_dir
                return _contained(source, candidate)
    if source.kind == "backup":
        if norm.startswith("~/") and source.state_source_path:
            home = str(PurePosixPath(source.state_source_path.replace("\\", "/")).parent)
            norm = home + norm[1:]
        m = re.match(r"^([A-Za-z]):/(.*)$", norm)
        if m:
            rel = Path("windows") / m.group(1).upper() / m.group(2)
        elif norm.startswith("/"):
            rel = Path("posix") / norm[1:]
        else:
            return None
        for payload in source.root.glob("*/payload"):
            candidate = payload / rel
            if candidate.exists():
                return _contained(source, candidate)
    return None


def _workspace_for_entry(source: BackupSource, entry: dict, defaults: dict) -> Path | None:
    agent_id = entry.get("id")
    # Defense in depth: list_agents already drops entries with a bad id before
    # calling this, but this builds `workspace-<id>` paths directly from it —
    # never trust a second call site not to skip that filter.
    if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id):
        return None
    raw = entry.get("workspace") if isinstance(entry.get("workspace"), str) else None
    is_default_agent = agent_id == "main" or entry.get("default") is True
    # The shared agents.defaults.workspace path is the main/default agent's
    # workspace, same restriction as the unsuffixed "workspace" dir fallback
    # below — an arbitrary non-default agent id must not silently inherit it.
    if raw is None and is_default_agent and isinstance(defaults.get("workspace"), str):
        raw = defaults["workspace"]
    if raw:
        resolved = _resolve_config_path(source, raw)
        if resolved is not None and resolved.is_dir():
            return resolved
    if source.state_dir is not None:
        candidates = [source.state_dir / f"workspace-{agent_id}"]
        if is_default_agent:
            candidates.append(source.state_dir / "workspace")
        for c in candidates:
            if c.is_dir():
                return c
    if len(source.workspace_dirs) == 1:
        return source.workspace_dirs[0]
    if source.state_dir is not None and not source.workspace_dirs and _has_persona(source.state_dir):
        return source.state_dir
    return None


def _display_name(entry: dict, ws: Path | None) -> str:
    identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
    for value in (entry.get("name"), identity.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _identity_md_field(ws, "Name") or entry["id"]


def _history_for(source: BackupSource, agent_id: str) -> HistoryInfo | None:
    if source.state_dir is None or not AGENT_ID_RE.match(agent_id):
        return None
    agent_root = source.state_dir / "agents" / agent_id
    sqlite = agent_root / "agent" / "openclaw-agent.sqlite"
    sessions = agent_root / "sessions"
    sq = sqlite if sqlite.is_file() else None
    sd = sessions if sessions.is_dir() and any(sessions.iterdir()) else None
    if sq is None and sd is None:
        return None
    size = (sq.stat().st_size if sq else 0) + sum(
        p.stat().st_size for p in (sd.rglob("*") if sd else []) if p.is_file()
    )
    return HistoryInfo(sqlite=sq, sessions_dir=sd, bytes=size)


def list_agents(source: BackupSource) -> list[AgentCandidate]:
    agents_block = source.config.get("agents") if isinstance(source.config.get("agents"), dict) else {}
    defaults = agents_block.get("defaults") if isinstance(agents_block.get("defaults"), dict) else {}
    entries = [
        e
        for e in (agents_block.get("list") or [])
        if isinstance(e, dict) and isinstance(e.get("id"), str) and AGENT_ID_RE.match(e["id"])
    ]
    if not entries:
        entries = [{"id": "main"}]
    out: list[AgentCandidate] = []
    for entry in entries:
        ws = _workspace_for_entry(source, entry, defaults)
        out.append(
            AgentCandidate(
                id=entry["id"],
                name=_display_name(entry, ws),
                entry=entry,
                workspace_dir=ws,
                history=_history_for(source, entry["id"]),
            )
        )
    return out


def list_plugins(source: BackupSource) -> list[PluginCandidate]:
    """Non-bundled plugins from ``plugins/installs.json`` (+ their source dir
    under ``extensions/`` and their scrubbed ``plugins.entries`` config)."""
    if source.state_dir is None:
        return []
    installs = source.state_dir / "plugins" / "installs.json"
    if not installs.is_file():
        return []
    try:
        if installs.stat().st_size > MAX_METADATA_BYTES:
            return []
        data = json.loads(installs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("plugins") if isinstance(data, dict) else None
    if isinstance(records, dict):
        records = list(records.values())
    if not isinstance(records, list):
        return []
    entries_cfg = (
        ((source.config.get("plugins") or {}).get("entries") or {})
        if isinstance(source.config.get("plugins"), dict)
        else {}
    )
    scrubbed_entries = (_scrub_plugins_block({"entries": entries_cfg}).get("entries") or {}) if entries_cfg else {}
    out: list[PluginCandidate] = []
    for rec in records:
        if not isinstance(rec, dict) or rec.get("origin") == "bundled":
            continue
        pid = rec.get("pluginId")
        if not isinstance(pid, str) or not AGENT_ID_RE.match(pid):
            continue
        src_dir: Path | None = None
        root_dir = rec.get("rootDir")
        if isinstance(root_dir, str):
            base = PurePosixPath(root_dir.replace("\\", "/")).name
            cand = source.state_dir / "extensions" / base
            if cand.is_dir():
                src_dir = cand
        if src_dir is None and (source.state_dir / "extensions" / pid).is_dir():
            src_dir = source.state_dir / "extensions" / pid
        has_deps = False
        if src_dir is not None and (src_dir / "package.json").is_file():
            pkg_path = src_dir / "package.json"
            try:
                if pkg_path.stat().st_size <= MAX_METADATA_BYTES:
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                    has_deps = bool(isinstance(pkg, dict) and pkg.get("dependencies"))
            except (OSError, ValueError):
                has_deps = False
        cfg = scrubbed_entries.get(pid) if isinstance(scrubbed_entries.get(pid), dict) else {"enabled": True}
        # Plugin configs are free-form (unlike MCP/skill entries, whose
        # upstream schema pins secrets at the top level — see
        # catalog_slice._scrub_secret_fields's docstring), so a nested
        # `config: {"auth": {"apiKey": ...}}` survives the shallow scrub
        # above. _scrub_tools_block already walks arbitrary nesting blanking
        # any secret-shaped key at any depth (same job the free-form `tools`
        # block needs) — reuse it here instead of a second bespoke walker.
        cfg = _scrub_tools_block(cfg)
        out.append(
            PluginCandidate(
                id=pid,
                package_name=str(rec.get("packageName") or pid),
                version=str(rec.get("packageVersion") or ""),
                source_dir=src_dir,
                has_dependencies=has_deps,
                config=cfg,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 4. Materialization
# ---------------------------------------------------------------------------


@dataclass
class ImportSelection:
    skills: set[str] | None = None  # None = all
    mcp_servers: set[str] | None = None  # None = all
    include_memory: bool = True
    include_cron: bool = True


@dataclass
class ImportNormalized:
    kind: str
    agent_id: str
    name: str
    agents: list[AgentCandidate]
    plugins: list[PluginCandidate]
    cron_jobs: list[dict]
    mcp_names: list[str]
    skill_names: list[str]
    warnings: list[str]


# State-relative top-level dirs treated as runtime (not content) when the
# agent's workspace_dir IS the state dir itself (an Isol8 artifact re-upload
# — see the module docstring's "candidate prefix" note). "memory" and "tools"
# are deliberately NOT here (R2): both are legitimate workspace content in
# that shape — an Isol8 artifact re-upload keeps persona files + memory/ at
# the same level as openclaw.json — and true state-dir memory/*.sqlite is
# already dropped by the streaming extraction's skip rules (_STATE_SKIP_DIRS).
_STATE_RUNTIME_DIRS = (
    "agents",
    "sessions",
    "logs",
    "credentials",
    "devices",
    "identity",
    "canvas",
    "completions",
    "delivery-queue",
    "session-delivery-queue",
    "state",
    "extensions",
    "plugins",
    "dev",
    "git",
    "npm",
    "npm-runtime",
    "cron",
    "browser",
    "media",
)


def _read_cron_jobs(state_dir: Path | None) -> list[dict]:
    if state_dir is None:
        return []
    path = state_dir / "cron" / "jobs.json"
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in (jobs or []) if isinstance(j, dict)] if isinstance(jobs, list) else []


def _copy_tree(src: Path, dst: Path, *, ignore_top: tuple[str, ...] = ()) -> None:
    def _ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in _UNIVERSAL_SKIP_DIRS or n in _UNIVERSAL_SKIP_FILES or n.startswith(".")}
        if Path(directory) == src:
            skipped.update(n for n in names if n in ignore_top)
        return skipped

    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False, ignore=_ignore)


def _scrubbed_config_blocks(config: dict, selection: ImportSelection) -> tuple[dict, dict, dict, dict]:
    mcp_src = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
    servers_src = mcp_src.get("servers") if isinstance(mcp_src.get("servers"), dict) else {}
    servers = {
        n: _scrub_secret_fields(s)
        for n, s in servers_src.items()
        if isinstance(s, dict) and (selection.mcp_servers is None or n in selection.mcp_servers)
    }
    mcp = {"servers": servers} if servers else {}
    skills_src = config.get("skills") if isinstance(config.get("skills"), dict) else {}
    skills: dict[str, Any] = {}
    entries_src = skills_src.get("entries") if isinstance(skills_src.get("entries"), dict) else {}
    entries = {
        n: _scrub_secret_fields(e)
        for n, e in entries_src.items()
        if isinstance(e, dict) and (selection.skills is None or n in selection.skills)
    }
    if entries:
        skills["entries"] = entries
    for key in ("allowBundled", "load"):
        if key in skills_src:
            skills[key] = skills_src[key]
    # Wrap the shallow plugin scrub with the same deep walk applied in
    # list_plugins — plugin entries carry free-form nested config that
    # _scrub_plugins_block's top-level-only pass does not reach.
    plugins = (
        _scrub_tools_block(_scrub_plugins_block(config["plugins"])) if isinstance(config.get("plugins"), dict) else {}
    )
    tools = _scrub_tools_block(config["tools"]) if isinstance(config.get("tools"), dict) else {}
    return mcp, skills, plugins, tools


def _local_path_hits(server: dict) -> list[str]:
    parts: list[str] = []
    if isinstance(server.get("command"), str):
        parts.append(server["command"])
    parts.extend(a for a in (server.get("args") or []) if isinstance(a, str))
    if isinstance(server.get("cwd"), str):
        parts.append(server["cwd"])
    return [p for p in parts if _LOCAL_PATH_RE.match(p)]


def materialize_staging(
    source: BackupSource,
    agent: AgentCandidate,
    candidates: list[AgentCandidate],
    staging_dir: Path,
    *,
    selection: ImportSelection,
    fleet_version: str,
) -> ImportNormalized:
    warnings: list[str] = []
    ws_out = staging_dir / "workspace"
    if ws_out.exists():
        shutil.rmtree(ws_out)
    ws_out.mkdir(parents=True)

    if agent.workspace_dir is not None:
        copying_state_dir = source.state_dir is not None and agent.workspace_dir == source.state_dir
        _copy_tree(agent.workspace_dir, ws_out, ignore_top=_STATE_RUNTIME_DIRS if copying_state_dir else ())
    else:
        warnings.append(f"no workspace directory found for agent {agent.id!r} — only its config entry was imported")

    if source.state_dir is not None and (source.state_dir / "skills").is_dir():
        for skill_dir in sorted(
            p for p in (source.state_dir / "skills").iterdir() if p.is_dir() and (p / "SKILL.md").is_file()
        ):
            dest = ws_out / "skills" / skill_dir.name
            if not dest.exists():
                _copy_tree(skill_dir, dest)
    if selection.skills is not None and (ws_out / "skills").is_dir():
        for d in list((ws_out / "skills").iterdir()):
            if d.is_dir() and d.name not in selection.skills:
                shutil.rmtree(d)
    workspace_skills = (
        sorted(p.name for p in (ws_out / "skills").iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
        if (ws_out / "skills").is_dir()
        else []
    )

    if not selection.include_memory:
        shutil.rmtree(ws_out / "memory", ignore_errors=True)
        (ws_out / "MEMORY.md").unlink(missing_ok=True)
    (ws_out / "openclaw.json").unlink(missing_ok=True)
    (ws_out / "build-manifest.json").unlink(missing_ok=True)

    entry = dict(agent.entry)
    entry["id"] = agent.id
    entry["name"] = agent.name
    if not entry.get("description"):
        vibe = _identity_md_field(agent.workspace_dir, "Vibe")
        if vibe:
            entry["description"] = vibe
    if "channels" in entry:
        warnings.append("channel bindings are not imported — reconnect channels in Isol8 after the agent lands")
    if isinstance(entry.get("model"), str) and entry["model"]:
        warnings.append(
            f"model {entry['model']!r} is not carried over — the agent runs on your Isol8 provider and model"
        )
    _sanitized, dropped = sanitize_agent_entry_for_schema(strip_user_specific_fields(entry))
    if dropped:
        warnings.append(
            "config keys this OpenClaw version does not accept were dropped from the agent entry: "
            + ", ".join(sorted(dropped))
        )

    mcp, skills, plugins, tools = _scrubbed_config_blocks(source.config, selection)
    for name, server in (mcp.get("servers") or {}).items():
        hits = _local_path_hits(server)
        if hits:
            warnings.append(
                f"MCP server {name!r} points at a path on your machine ({hits[0]}) — it will not resolve inside the container; edit it after import"
            )
    meta = source.config.get("meta") if isinstance(source.config.get("meta"), dict) else {}
    src_version = meta.get("lastTouchedVersion")
    if isinstance(src_version, str) and src_version and src_version.split("-", 1)[0] != fleet_version.split("-", 1)[0]:
        warnings.append(
            f"backup was written by OpenClaw {src_version}; Isol8 runs {fleet_version} — unsupported config keys are dropped, not failed"
        )

    persisted_entry = {"id": agent.id, **_sanitized}
    (ws_out / "openclaw.json").write_text(
        json.dumps(
            {"agents": {"list": [persisted_entry]}, "mcp": mcp, "skills": skills, "plugins": plugins, "tools": tools},
            indent=2,
        ),
        encoding="utf-8",
    )

    jobs = _read_cron_jobs(source.state_dir) if selection.include_cron else []
    if len(candidates) == 1:
        for job in jobs:
            job.setdefault("agentId", agent.id)
    cron_jobs = filter_cron_jobs_for_agent(jobs, agent.id)
    (staging_dir / "cron-jobs.json").write_text(json.dumps(cron_jobs), encoding="utf-8")

    mcp_names = sorted((mcp.get("servers") or {}).keys())
    skill_names = sorted(set((skills.get("entries") or {}).keys()) | set(workspace_skills))
    plugin_candidates = list_plugins(source)
    (staging_dir / "import-manifest.json").write_text(
        json.dumps(
            {
                "kind": source.kind,
                "agent_id": agent.id,
                "name": agent.name,
                "agents": [{"id": c.id, "name": c.name, "has_history": c.history is not None} for c in candidates],
                "plugins": [
                    {"id": p.id, "spec": f"{p.package_name}@{p.version}", "has_dependencies": p.has_dependencies}
                    for p in plugin_candidates
                ],
                "mcp_servers": mcp_names,
                "skills": skill_names,
                "cron_jobs": len(cron_jobs),
                "warnings": warnings,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return ImportNormalized(
        kind=source.kind,
        agent_id=agent.id,
        name=agent.name,
        agents=candidates,
        plugins=plugin_candidates,
        cron_jobs=cron_jobs,
        mcp_names=mcp_names,
        skill_names=skill_names,
        warnings=warnings,
    )


def _pick(candidates: list[AgentCandidate], agent_id: str | None) -> AgentCandidate:
    if agent_id is not None:
        for c in candidates:
            if c.id == agent_id:
                return c
        raise BackupImportError(f"agent {agent_id!r} is not in this backup")
    for c in candidates:
        if c.entry.get("default") is True:
            return c
    for c in candidates:
        if c.id == "main":
            return c
    return candidates[0]


def restage(
    staging_dir: Path, *, agent_id: str | None, selection: ImportSelection, fleet_version: str
) -> ImportNormalized:
    source = locate_source(staging_dir / "raw")
    candidates = list_agents(source)
    return materialize_staging(
        source, _pick(candidates, agent_id), candidates, staging_dir, selection=selection, fleet_version=fleet_version
    )


def stage_upload(
    open_stream: Callable[[], BinaryIO], staging_dir: Path, *, fleet_version: str
) -> tuple[ImportNormalized, ExtractReport]:
    report = extract_archive(open_stream, staging_dir / "raw")
    return restage(staging_dir, agent_id=None, selection=ImportSelection(), fleet_version=fleet_version), report
