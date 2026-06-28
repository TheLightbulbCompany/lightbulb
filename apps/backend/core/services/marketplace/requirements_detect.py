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
_SKIP_SCAN_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv", ".cache", "uploads"})

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
        # POSIX shell builtins + coreutils: ubiquitous plumbing present on every
        # image, never a buyer-facing dependency. A pipeline segment headed by
        # one of these (`printf …`, `grep …`, `cd x && rm y`) is glue, not a tool.
        "echo",
        "printf",
        "cat",
        "set",
        "export",
        "printenv",
        "cd",
        "pwd",
        "source",
        "eval",
        "exit",
        "true",
        "false",
        "test",
        "sleep",
        "read",
        "wait",
        "trap",
        "kill",
        "which",
        "env",
        "time",
        "grep",
        "sed",
        "awk",
        "cut",
        "tr",
        "sort",
        "uniq",
        "head",
        "tail",
        "wc",
        "tee",
        "xargs",
        "seq",
        "ls",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "chmod",
        "chown",
        "ln",
        "basename",
        "dirname",
        "realpath",
        "mktemp",
        "date",
        "find",
        "tar",
        "gzip",
        "gunzip",
        "zip",
        "unzip",
        "base64",
        # Lobster durable-KV DSL verb (`pipeline: state.get/state.set`), not a
        # buyer-facing tool dep — the engine ships it, it's never a dependency.
        "state",
    }
)

_TABLE_PATH = Path(__file__).with_name("known_services.yaml")


@dataclasses.dataclass(frozen=True)
class Hit:
    kind: str  # "tool" | "env" | "host" | "cli_pkg"
    value: str
    source: str


def detect_in_text(text: str, source: str, *, include_tools: bool = True) -> list[Hit]:
    """Pull tool/cli/env/host references out of a file's text.

    ``include_tools`` gates the tool-flag (``--tool X``) and ``npx``/``uvx``
    package scans. Those run ONLY on pipeline files (``.lobster``/``.yaml``),
    where they reflect a declared workflow step. Helper sources (``.js``/``.ts``
    /``.md``) are the agent's own implementation — their internal ``npx tsc`` /
    ``npx vitest`` and stray tokens are NOT buyer-facing dependencies, so they'd
    only pollute the ledger. Env/host (the known-service mapping that surfaces
    real integrations like Stripe/Google) always run, on every scanned file.
    """
    hits: list[Hit] = []
    if include_tools:
        for m in _TOOL_FLAG.finditer(text):
            hits.append(Hit("tool", m.group(1), source))
        for m in _NPX.finditer(text):
            hits.append(Hit("cli_pkg", m.group(1), source))
    for m in _ENV.finditer(text):
        hits.append(Hit("env", m.group(1), source))
    for m in _HOST.finditer(text):
        hits.append(Hit("host", m.group(1), source))
    return hits


# JS/TS keywords that show up as the first token of an inline script body
# (`node -e 'const x = …'`, `return foo()`). They are never tool/plugin names,
# so they're rejected as command heads even if a split somehow exposes them.
_JS_KEYWORDS = frozenset(
    {
        "const",
        "let",
        "var",
        "return",
        "try",
        "catch",
        "typeof",
        "for",
        "new",
        "if",
        "else",
        "while",
        "function",
        "async",
        "await",
        "do",
        "switch",
        "case",
        "break",
        "continue",
        "throw",
        "yield",
        "delete",
        "void",
        "in",
        "of",
        "instanceof",
        "class",
        "this",
        "super",
        "import",
        "export",
        "default",
        "from",
        "null",
        "true",
        "false",
        "undefined",
    }
)


def _split_top_level(command: str) -> list[str]:
    """Split a command line on the shell chain separators ``||``, ``&&``, ``|``,
    and ``;`` — but ONLY at quote-depth 0.

    The whole point is that an inline script passed to a runner with ``-e`` /
    ``-c`` is a SINGLE quoted argument, NOT a chain of shell commands:
    ``node -e 'const a=1; foo(); process.exit()'`` is one ``node`` invocation
    whose ``;`` (and any ``|``) are JavaScript/Python syntax living inside the
    quotes. A naive regex split on ``;``/``|`` cuts through that quoted body and
    hands back ``const``/``process``/… as bogus "tool" heads. Tracking quote
    state and splitting only outside quotes keeps the inline body intact, so its
    head stays the runner (``node``/``python``, both plumbing heads we drop).

    Walks char by char, toggling quote state on an unescaped ``'`` or ``"`` and
    cutting only when not inside a quote. ``&`` alone is not a separator (only
    ``&&`` is), matching the previous regex behaviour.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                # Escaped char can't close the quote — keep it verbatim.
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "|":
            # `||` or a bare `|` — both chain commands at the shell level.
            segments.append("".join(buf))
            buf = []
            i += 2 if (i + 1 < n and command[i + 1] == "|") else 1
            continue
        if ch == "&" and i + 1 < n and command[i + 1] == "&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _tool_heads(command: str) -> list[str]:
    """Buyer-relevant tools a command line invokes via DIRECT CLI form, e.g.
    ``gog.gmail.search --query …`` → ``gog`` and ``cd /app && inbox list`` →
    ``inbox``. Splits on shell separators (``|``, ``&&``, ``;``) — quote-aware,
    so an inline ``node -e '…;…'`` script body is one segment, not chopped on the
    ``;`` inside its quotes — and returns the head of each segment.

    Skips execution plumbing (``openclaw.invoke``, ``exec``, shell runners):
    ``--tool`` already captures the wrapped tool and curl/wget hosts are caught
    by the host regex. The leading segment of a dotted head is the tool/plugin
    (``gog.gmail.search`` is the ``gog`` plugin).
    """
    heads: list[str] = []
    for segment in _split_top_level(command):
        stripped = segment.strip().lstrip("(")
        if not stripped:
            continue
        head = stripped.split(maxsplit=1)[0].split(".", 1)[0]
        # Reject plumbing heads, JS/TS keywords, single chars, and non-identifier
        # junk. The keyword/short-name guard is defense-in-depth: the quote-aware
        # split already keeps an inline script body whole, but should a split ever
        # leak a script token we don't want `const`/`let`/`x` posing as a tool.
        if head and len(head) >= 2 and head not in _NON_TOOL_HEADS and head not in _JS_KEYWORDS and _NAME.match(head):
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
        # Tool/cli scraping is scoped to pipeline files; helper sources
        # (.js/.ts/.md) contribute only env/host (real-integration) signal.
        is_pipeline = path.suffix in _YAML_SUFFIXES
        hits.extend(detect_in_text(text, source=rel.name, include_tools=is_pipeline))
        # Pipeline files are YAML: also pull direct-CLI tool heads
        # (gog.gmail.search, inbox list) that the --tool regex can't see.
        if is_pipeline:
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
