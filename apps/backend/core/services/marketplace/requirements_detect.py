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

_SCAN_GLOBS = (
    "pipelines/*.lobster",
    "pipelines/*.yaml",
    "pipelines/*.yml",
    "scripts/*",
    "*.md",
)

_TABLE_PATH = Path(__file__).with_name("known_services.yaml")


@dataclasses.dataclass(frozen=True)
class Hit:
    kind: str  # "tool" | "env" | "host"
    value: str
    source: str


def detect_in_text(text: str, source: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in _TOOL_FLAG.finditer(text):
        hits.append(Hit("tool", m.group(1), source))
    for m in _ENV.finditer(text):
        hits.append(Hit("env", m.group(1), source))
    for m in _HOST.finditer(text):
        hits.append(Hit("host", m.group(1), source))
    return hits


def detect_in_tree(agent_dir: Path) -> list[Hit]:
    hits: list[Hit] = []
    for pattern in _SCAN_GLOBS:
        for path in sorted(agent_dir.glob(pattern)):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits.extend(detect_in_text(text, source=path.name))
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
