"""Detect and normalize an uploaded Claude Code context (``~/.claude``) into
the staging layout the import wizard consumes.

Accepted archive shapes (both must carry project memory to be detected):

  dot-claude — a tar of the home dir with everything under ``.claude/``
               (``tar czf ctx.tgz .claude`` from ``$HOME``).
  bare       — a tar taken from inside ``~/.claude`` itself (``CLAUDE.md`` /
               ``projects/`` / ``skills/`` at the archive root).

Detection is deliberately anchored on ``projects/<entry>/memory/*.md`` — the
one shape unique to a Claude Code home. A ``CLAUDE.md`` + ``skills/`` tar
without project memory collides with a plain agent-workspace tar (OpenClaw
loads a root ``CLAUDE.md`` as workspace context) and must keep routing to the
backup/workspace import path, so it is NOT detected here. Persona markers
(``AGENTS.md`` etc.) and the ``openclaw backup create`` layout
(``<dir>/manifest.json`` + ``<dir>/payload/``) likewise force a ``None``.

Extraction is ALLOWLIST-ONLY, one streaming pass: project memory markdown,
the root ``CLAUDE.md``, and ``skills/<name>/**`` are staged; everything else
is counted in ``skipped`` by reason and never touches disk — credentials
(``.credentials.json``, ``settings*.json``), transcripts (``*.jsonl``), and
runtime state must never be written, even transiently. A traversal member
(absolute path or a ``..`` escape) aborts the whole import — those only
appear in crafted archives, and a partial extraction of a hostile upload is
worthless. Link members are merely skipped (see ``_check_member``): real
home-dir tars legitimately carry symlinks, and a skipped link is never
followed, so skipping is strictly safe.

Staged layout under ``dst`` (a scratch tree; the apply half reads it back and uploads the
result into the target agent's S3 profile)::

    claude/
      memory/<collection-id>/<relpath>   projects/<slug>/memory/**/*.md
      skills/<name>/**
      CLAUDE.md
    import-manifest.json                 {"kind": "claude", ...}; no safety
                                         flags — those are preview-only

Collection ids are ``<sanitized-slug>-<sha256(slug)[:10]>``: the hash keeps
ids STABLE across re-imports (upstream hashes the realpath the same way), so
a re-import is a clean refresh, never an orphaned copy.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core.services.marketplace_safety import SafetyFlag, scan

# Upload + stream bounds. They used to live in ``openclaw_backup_import``, which went with the
# OpenClaw-backup import rail; this module is the only importer left, so they live here.
MAX_UPLOAD_BYTES = 2 * 1024**3  # S3 presigned PUT; far above any real ~/.claude
MAX_MEMBERS = 200_000
IMPORT_ID_RE = re.compile(r"^import-[0-9a-f]{12}$")

#: A tar carrying one of these at the root is an AGENT workspace, not a Claude home -- see
#: ``detect_claude_root``, which refuses rather than importing someone's agent as a context.
_PERSONA_MARKERS = ("AGENTS.md", "SOUL.md", "IDENTITY.md")
_UNIVERSAL_SKIP_DIRS = frozenset({"node_modules", ".git", "__pycache__"})


class BackupImportError(ValueError):
    """A member-level archive fault; message is user-facing."""


def _check_member(member: tarfile.TarInfo, dst_resolved: Path, dst: Path) -> Path | None:
    """Validate one member; None means SKIP it (never extract).

    Link members are skipped, not fatal: real home-dir tars legitimately carry symlinks, and a
    skipped link is never followed or extracted, so skipping is strictly safe while hard-failing
    would reject real-world archives outright. Absolute paths and traversal stay fatal -- those
    only appear in crafted archives, and a partial extraction of a hostile upload is worthless.
    """
    if member.name.startswith("/"):
        raise BackupImportError(f"archive member has an absolute path: {member.name!r}")
    target = (dst / member.name).resolve()
    try:
        target.relative_to(dst_resolved)
    except ValueError as exc:
        raise BackupImportError(f"archive member escapes the extraction directory: {member.name!r}") from exc
    if member.issym() or member.islnk():
        return None
    return target


# The backup extractor's 512 MB/2 GB caps are pointless for markdown; these
# are sized for a real ~/.claude (upstream caps memory discovery at 2000
# files too).
CLAUDE_MEMORY_MAX_FILES = 2000
CLAUDE_MAX_MD_BYTES = 1 * 1024**2  # memory files + CLAUDE.md; over → skipped "too_large"
CLAUDE_MAX_SKILL_FILE_BYTES = 5 * 1024**2
CLAUDE_MAX_TOTAL_BYTES = 200 * 1024**2  # over → raise

_MEMORY_MD_RE = re.compile(r"projects/[^/]+/memory/.+\.md")
_SLUG_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ClaudeImportError(ValueError):
    """The upload is not a Claude context we can import; message is user-facing."""


@dataclass
class ClaudeCollection:
    id: str
    label: str
    file_count: int


@dataclass
class ClaudeNormalized:
    collections: list[ClaudeCollection]
    has_user_memory: bool
    skills: list[str]
    skipped: dict[str, int]
    safety_flags: list[SafetyFlag]


def detect_claude_root(names: list[str]) -> str | None:
    """The archive prefix under which ``.claude`` content lives (``""`` or
    ``".claude/"``), or ``None`` if the archive is not Claude-shaped."""
    stripped = [n.removeprefix("./") for n in names]
    if any(n in _PERSONA_MARKERS for n in stripped):
        return None  # workspace tar
    manifest_dirs = {
        n[: -len("/manifest.json")] for n in stripped if n.endswith("/manifest.json") and n.count("/") == 1
    }
    if any(n.startswith(f"{d}/payload/") for d in manifest_dirs for n in stripped):
        return None  # openclaw backup layout
    root = ".claude/" if any(n.startswith(".claude/") for n in stripped) else ""
    for n in stripped:
        if not n.startswith(root):
            continue
        rel = n[len(root) :]
        if _MEMORY_MD_RE.fullmatch(rel):
            return root
        # A ``.claude/`` prefix is itself the proof: the dialog's own command archives
        # ``.claude/CLAUDE.md .claude/projects/*/memory .claude/skills`` "whichever exist", so
        # CLAUDE.md or a skill alone is valid there. Only the BARE layout still needs a memory
        # file, because bare CLAUDE.md + skills is also a plain workspace shape.
        if root and (rel == "CLAUDE.md" or (rel.startswith("skills/") and rel.count("/") >= 2)):
            return root
    return None


def _sanitize_slug(slug: str) -> str:
    out = _SLUG_DISALLOWED_RE.sub("-", slug).strip("._-")[:72]
    return out or "project"


def _collection_id(slug: str) -> str:
    return f"{_sanitize_slug(slug)}-{hashlib.sha256(slug.encode()).hexdigest()[:10]}"


def _classify(parts: list[str]) -> tuple[str, Path | None]:
    """(skip_reason, None) or ("", staged-relative path) for one member.

    Order matters: the credential/transcript nets run before the allowlist so
    a secrets file can never be rescued by also matching an allowlisted shape.
    """
    base = parts[-1]
    if base == ".credentials.json" or (base.startswith("settings") and base.endswith(".json")):
        return "credentials", None
    if parts[0] == "projects" and base.endswith(".jsonl"):
        return "transcripts", None
    if parts[0] == "commands":
        # Upstream imports commands/ as skills; cutting them here is
        # deliberate, so label the cut honestly rather than folding it
        # into "runtime".
        return "commands", None
    if parts[0] == "projects" and len(parts) >= 4 and parts[2] == "memory":
        if not base.endswith(".md"):
            return "non_markdown_memory", None
        return "", Path("claude", "memory", _collection_id(parts[1]), *parts[3:])
    if parts == ["CLAUDE.md"]:
        return "", Path("claude", "CLAUDE.md")
    if parts[0] == "skills" and len(parts) >= 3:
        if any(p.startswith(".") or p in _UNIVERSAL_SKIP_DIRS for p in parts[1:]):
            return "runtime", None
        return "", Path("claude", *parts)
    return "runtime", None


def extract_claude_archive(open_stream: Callable[[], BinaryIO], dst: Path) -> ClaudeNormalized:
    """Allowlist-extract a Claude context tar into ``dst``. ``open_stream``
    must return a fresh readable (an S3 body or a BytesIO)."""
    dst.mkdir(parents=True, exist_ok=True)
    dst_resolved = dst.resolve()

    collections: dict[str, ClaudeCollection] = {}  # slug → collection, insertion-ordered
    skills: list[str] = []
    skipped: dict[str, int] = {}
    has_user_memory = False
    memory_files = 0
    total_bytes = 0
    count = 0

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    try:
        with tarfile.open(fileobj=open_stream(), mode="r|gz") as tf:
            for member in tf:
                count += 1
                if count > MAX_MEMBERS:
                    raise ClaudeImportError(f"archive has more than {MAX_MEMBERS} entries")
                try:
                    if _check_member(member, dst_resolved, dst) is None:
                        _skip("links")
                        continue
                except BackupImportError as exc:
                    raise ClaudeImportError(str(exc)) from exc
                name = member.name.removeprefix("./").removeprefix(".claude/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts:
                    continue
                if ".." in parts:
                    # _check_member only rejects a `..` that escapes dst as
                    # written; we REMAP paths, so any `..` could escape the
                    # remapped root. Traversal aborts — crafted archives get
                    # nothing, not a partial import.
                    raise ClaudeImportError(f"archive member escapes the extraction directory: {member.name!r}")
                if member.isdir():
                    continue
                if not member.isfile():
                    _skip("runtime")  # device/fifo nodes — never extract
                    continue
                reason, rel = _classify(parts)
                if rel is None:
                    _skip(reason)
                    continue
                is_skill = parts[0] == "skills"
                if member.size > (CLAUDE_MAX_SKILL_FILE_BYTES if is_skill else CLAUDE_MAX_MD_BYTES):
                    _skip("too_large")
                    continue
                total_bytes += member.size
                if total_bytes > CLAUDE_MAX_TOTAL_BYTES:
                    raise ClaudeImportError("archive expands past the extraction cap")
                src = tf.extractfile(member)
                if src is None:
                    continue
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                if parts[0] == "projects":
                    memory_files += 1
                    if memory_files > CLAUDE_MEMORY_MAX_FILES:
                        raise ClaudeImportError(f"more than {CLAUDE_MEMORY_MAX_FILES} memory files")
                    slug = parts[1]
                    if slug not in collections:
                        collections[slug] = ClaudeCollection(id=_collection_id(slug), label=slug, file_count=0)
                    collections[slug].file_count += 1
                elif is_skill:
                    if parts[1] not in skills:
                        skills.append(parts[1])
                else:
                    has_user_memory = True
    except tarfile.TarError as exc:
        raise ClaudeImportError("not a valid .tar.gz archive") from exc
    if count == 0:
        raise ClaudeImportError("archive is empty")

    (dst / "import-manifest.json").write_text(
        json.dumps(
            {
                "kind": "claude",
                "collections": [
                    {"id": c.id, "label": c.label, "file_count": c.file_count} for c in collections.values()
                ],
                "has_user_memory": has_user_memory,
                "skills": skills,
                "skipped": skipped,
            }
        ),
        encoding="utf-8",
    )

    # Informational only — it is the owner's own workspace, so flags never
    # block. "skillmd" covers the credential patterns without the persona
    # email/phone PII patterns (memory dumps are full of emails; a wall of
    # email-address flags would drown the signal).
    staged_md = sorted(p for p in (dst / "claude").rglob("*.md") if p.is_file())
    safety_flags = scan(
        {str(p.relative_to(dst)): p.read_bytes()[: 1024**2] for p in staged_md},
        format="skillmd",
    )

    return ClaudeNormalized(
        collections=list(collections.values()),
        has_user_memory=has_user_memory,
        skills=skills,
        skipped=skipped,
        safety_flags=safety_flags,
    )


# ---------------------------------------------------------------------------
# Apply half — confirm moves the staged content into one agent's workspace.

# NOTE: the 1000:1000 chown these writes used to carry went with the ECS runtime. The apply
# target is a scratch tree the backend owns and then uploads into the agent's S3 profile; no
# uid ever opens these paths directly. The staged ``CLAUDE.md`` is NOT applied here: the caller
# merges it into ``memories/USER.md`` through ``context_import.merge_context``, the same
# entry-level merge the context paste door uses.


def apply_to_workspace(
    staging_dir: Path,
    workspace_dir: Path,
    collections: list[str] | None,
    skills: list[str] | None,
) -> list[dict]:
    """Apply a staged claude import into one agent's profile tree. ``collections``/
    ``skills`` filter by id/name; ``None`` means all. Returns ``[{"item", "status"}]`` rows.

    Memory lands under ``memory/imports/claude-code/<cid>/`` — upstream's own
    ``openclaw migrate claude`` namespace, so a user who ran the local
    migration AND this import gets ONE tree, not two. Cids are hash-stable, so
    a re-import replaces each selected collection in place (rmtree + copy — a
    clean refresh, never an orphaned copy). Skills never overwrite: an
    existing ``skills/<name>/`` is reported ``skipped_exists`` and left alone.
    """
    src = staging_dir / "claude"
    results: list[dict] = []

    wanted_collections = None if collections is None else set(collections)
    memory_src = src / "memory"
    memory_dirs = sorted(p for p in memory_src.iterdir() if p.is_dir()) if memory_src.is_dir() else []
    for cid_dir in memory_dirs:
        if wanted_collections is not None and cid_dir.name not in wanted_collections:
            continue
        target = workspace_dir / "memory" / "imports" / "claude-code" / cid_dir.name
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cid_dir, target)
        results.append({"item": f"memory:{cid_dir.name}", "status": "applied"})

    wanted_skills = None if skills is None else set(skills)
    skills_src = src / "skills"
    skill_dirs = sorted(p for p in skills_src.iterdir() if p.is_dir()) if skills_src.is_dir() else []
    for skill_dir in skill_dirs:
        if wanted_skills is not None and skill_dir.name not in wanted_skills:
            continue
        target = workspace_dir / "skills" / skill_dir.name
        if target.exists():
            results.append({"item": f"skill:{skill_dir.name}", "status": "skipped_exists"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, target)
        results.append({"item": f"skill:{skill_dir.name}", "status": "applied"})

    return results
