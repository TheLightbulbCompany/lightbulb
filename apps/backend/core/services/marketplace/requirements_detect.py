"""Dependency detectors — body-parse + service identification.

The ``.lobster`` pipeline DSL and ``openclaw.invoke --tool`` convention are
bespoke to OpenClaw, so no third-party tool parses them; this is the
irreducibly-ours layer. We scan pipeline/script/markdown bodies for tool
invocations, ``$ENV`` references, and external hosts, then map hosts/env to a
named service via a Gitleaks-seeded (MIT) table.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from pathlib import Path

import yaml

# `--tool <name>` (covers `openclaw.invoke --tool X` and bare `--tool X`).
_TOOL_FLAG = re.compile(r"--tool\s+([a-z0-9][a-z0-9._-]*)")
# $NAME or ${NAME}: uppercase start, [A-Z0-9_], length >= 3 total.
_ENV = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")
# scheme://host — capture the host (must contain a dot + TLD).
_HOST = re.compile(r"https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
# A tool name: lowercase, identifier-shaped.
_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
# `npx [-flags] <pkg>` / `uvx [-flags] <pkg>`: the runtime-resolved package an
# agent invokes without a persistent install. Captures the package spec
# (optionally @scoped) and stops before any @version — so `npx -y @stripe/mcp@1`
# yields `@stripe/mcp`. These re-resolve on the buyer's container on demand, so
# they ride along for free (no shipping needed) — see classify's cli_pkg branch.
_NPX = re.compile(
    r"\b(?:npx|uvx)\s+(?:-{1,2}[a-z][a-z0-9-]*\s+)*"
    r"(@?[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)?)",
    re.IGNORECASE,
)

# Fallback recursive globs (used when no explicit shipped-file list is given —
# e.g. the backfill over an already-filtered extracted tarball). `.lobster`
# pipelines live at the ROOT or under pipelines/; agents also keep invocations
# in scripts/, skills/, and markdown anywhere.
_SCAN_GLOBS = (
    "**/*.lobster",
    "**/*.yaml",
    "**/*.yml",
    "scripts/**/*",
    "skills/**/*",
    "**/*.md",
)

# File suffixes that can carry tool/env/host references. A path under a
# ``scripts/`` segment is scanned regardless of suffix (extensionless scripts).
_SCANNABLE_SUFFIXES = frozenset(
    {".lobster", ".yaml", ".yml", ".md", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".py", ".txt"}
)
# Dirs never scanned — mirrors the export tarball filter so the ledger can't
# disclose a dep from a file that won't ship. Dotfiles/dotdirs are skipped too.
_SKIP_SCAN_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv", ".cache"})

# YAML keys whose string values are command lines in the .lobster DSL.
_COMMAND_KEYS = frozenset({"command", "run", "pipeline", "cmd"})
# Pipeline files are YAML.
_YAML_SUFFIXES = (".lobster", ".yaml", ".yml")
# Command heads that are execution plumbing, not buyer-relevant tool deps:
# the openclaw.invoke wrapper (its real tool comes via --tool), shell runners,
# and common shell builtins. Hosts referenced via curl/wget are captured by the
# host regex; their command head is irrelevant.
_NON_TOOL_HEADS = frozenset(
    {
        "openclaw",
        "exec",
        "node",
        "python",
        "python3",
        "bash",
        "sh",
        "deno",
        "bun",
        "npx",
        "uv",
        "uvx",
        "curl",
        "wget",
        "jq",
        "echo",
        "cat",
        "set",
        "export",
        "cd",
        "source",
        "true",
        "false",
        "sleep",
        "test",
    }
)

_TABLE_PATH = Path(__file__).with_name("known_services.yaml")


@dataclasses.dataclass(frozen=True)
class Hit:
    kind: str  # "tool" | "env" | "host" | "cli_pkg"
    value: str
    source: str


def detect_in_text(text: str, source: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in _TOOL_FLAG.finditer(text):
        hits.append(Hit("tool", m.group(1), source))
    for m in _NPX.finditer(text):
        hits.append(Hit("cli_pkg", m.group(1), source))
    for m in _ENV.finditer(text):
        hits.append(Hit("env", m.group(1), source))
    for m in _HOST.finditer(text):
        hits.append(Hit("host", m.group(1), source))
    return hits


# Shell-level separators that chain independent commands. Splitting a command
# value on these lets us see the tool AFTER a `cd x && tool` or `a; b` prefix,
# not just the first head. `_NAME` + `_NON_TOOL_HEADS` reject any junk a split
# through a quoted/JSON arg might produce.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;]")


def _tool_heads(command: str) -> list[str]:
    """Buyer-relevant tools a command line invokes via DIRECT CLI form, e.g.
    ``gog.gmail.search --query …`` → ``gog`` and ``cd /app && inbox list`` →
    ``inbox``. Splits on shell separators (``|``, ``&&``, ``;``) and returns the
    head of each segment.

    Skips execution plumbing (``openclaw.invoke``, ``exec``, shell runners):
    ``--tool`` already captures the wrapped tool and curl/wget hosts are caught
    by the host regex. The leading segment of a dotted head is the tool/plugin
    (``gog.gmail.search`` is the ``gog`` plugin).
    """
    heads: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        stripped = segment.strip().lstrip("(")
        if not stripped:
            continue
        head = stripped.split(maxsplit=1)[0].split(".", 1)[0]
        if head and head not in _NON_TOOL_HEADS and _NAME.match(head):
            heads.append(head)
    return heads


def _detect_in_yaml(text: str, source: str) -> list[Hit]:
    """Structurally walk a .lobster/pipeline YAML and pull the tool head out of
    every ``command:``/``run:``/``pipeline:`` value — string or list-of-strings,
    and shell-chained (``|``/``&&``/``;``) segments. Falls back to text-only
    detection on a parse error.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    hits: list[Hit] = []

    def _emit(command: str) -> None:
        for head in _tool_heads(command):
            hits.append(Hit("tool", head, source))

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _COMMAND_KEYS:
                    if isinstance(value, str):
                        _emit(value)
                    elif isinstance(value, list):
                        # A command expressed as a YAML sequence of command lines.
                        for item in value:
                            if isinstance(item, str):
                                _emit(item)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return hits


def _scannable_rel(rel: Path) -> bool:
    """A workspace-relative path is scanned when it carries a scannable suffix
    (or sits under a ``scripts/`` dir) AND none of its segments is a junk dir or
    a dotfile/dotdir."""
    parts = rel.parts
    for p in parts:
        if p in _SKIP_SCAN_DIRS or p.startswith("."):
            return False
    return rel.suffix in _SCANNABLE_SUFFIXES or "scripts" in parts


def detect_in_tree(agent_dir, scan_files: list[str] | None = None) -> list[Hit]:
    """Scan an agent dir for tool/env/host/cli references.

    ``scan_files`` (the export tarball's shipped-file list) scopes the scan to
    EXACTLY what ships — so the ledger never discloses a dep from a file the
    export filter drops. When omitted, fall back to a filtered recursive walk
    (used by the backfill over an already-filtered extracted tarball).
    """
    agent_dir = Path(agent_dir)
    rels: list[Path] = []
    if scan_files is not None:
        for raw in scan_files:
            rel = Path(raw[2:] if raw.startswith("./") else raw)
            if _scannable_rel(rel):
                rels.append(rel)
    else:
        for pattern in _SCAN_GLOBS:
            for path in agent_dir.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(agent_dir)
                except ValueError:
                    continue
                if _scannable_rel(rel):
                    rels.append(rel)

    hits: list[Hit] = []
    seen: set[Path] = set()
    for rel in sorted(set(rels), key=str):
        if rel in seen:
            continue
        seen.add(rel)
        path = agent_dir / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits.extend(detect_in_text(text, source=rel.name))
        # Pipeline files are YAML: also pull direct-CLI tool heads
        # (gog.gmail.search, inbox list) that the --tool regex can't see.
        if path.suffix in _YAML_SUFFIXES:
            hits.extend(_detect_in_yaml(text, source=rel.name))
    return hits


@functools.lru_cache(maxsize=1)
def _service_table() -> dict:
    with _TABLE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def identify_service(hit: Hit) -> tuple[str, str] | None:
    """Map a host/env Hit to ``(canonical_key, display_name)`` or None."""
    table = _service_table()
    if hit.kind == "host":
        for key, spec in table.items():
            for suffix in spec.get("hosts", []) or []:
                if hit.value == suffix or hit.value.endswith("." + suffix):
                    return key, spec["display_name"]
    elif hit.kind == "env":
        for key, spec in table.items():
            for prefix in spec.get("env_prefixes", []) or []:
                if hit.value.startswith(prefix):
                    return key, spec["display_name"]
    return None
