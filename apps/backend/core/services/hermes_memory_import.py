"""Port of Hermes's own OpenClaw->Hermes memory translator (M6-R47).

``scripts/migrate_workspace_to_hermes.py`` carries an OpenClaw agent's ``MEMORY.md`` and
``memory/`` daily-notes directory verbatim into a Hermes profile, but Hermes never reads either
location: its curated memory lives at ``$HERMES_HOME/memories/MEMORY.md`` (agent notes) and
``$HERMES_HOME/memories/USER.md`` (user profile) -- see ``tools/memory_tool.py::get_memory_dir``
in the Hermes source. This module reproduces the entry-extraction/merge semantics of Hermes's own
migration tool so the migration job can write memory where Hermes actually reads it.

Ported (attribution, MIT) from ``optional-skills/migration/openclaw-migration/scripts/
openclaw_to_hermes.py`` in ``NousResearch/hermes-agent`` at tag ``v2026.9.14`` (package 0.21.3):
``extract_markdown_entries``, ``merge_entries``, and the two char-limit constants below are ports
of that file's functions of the same name. ``ENTRY_DELIMITER`` is ported from
``tools/memory_tool_store.py`` at the same tag (verified against the pinned source, not just the
migration tool's own copy of the constant). ``rebrand_text`` (OpenClaw-wording rewrite) is
deliberately NOT ported -- we keep carried content verbatim.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

#: Ported verbatim from Hermes ``tools/memory_tool_store.py`` (tag v2026.9.14) -- the separator
#: ``MemoryStore`` joins/splits curated memory entries on. Must match exactly: a different
#: delimiter here would make every entry this job writes read back as one giant unsplittable blob.
ENTRY_DELIMITER = "\n§\n"

#: Ported verbatim from the Hermes migration tool's ``DEFAULT_MEMORY_CHAR_LIMIT``/
#: ``DEFAULT_USER_CHAR_LIMIT``.
MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$")
_OWN_FILE_HEADING_RE = re.compile(r"\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY)\.md\b", re.I)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_markdown_entries(text: str) -> list[str]:
    """Turn a free-form markdown file into memory entries: one per paragraph or bullet, prefixed
    with its heading context (``"H1 > H2: text"``), skipping fenced code blocks and table rows,
    and dropping headings that are just a source-file name (``MEMORY.md`` etc.) from the prefix.
    Ported from the Hermes migration tool's function of the same name."""
    entries: list[str] = []
    headings: list[str] = []
    paragraph_lines: list[str] = []

    def context_prefix() -> str:
        filtered = [h for h in headings if h and not _OWN_FILE_HEADING_RE.search(h)]
        return " > ".join(filtered)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        text_block = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines = []
        if not text_block:
            return
        prefix = context_prefix()
        entries.append(f"{prefix}: {text_block}" if prefix else text_block)

    in_code_block = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            flush_paragraph()
            continue
        if in_code_block:
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            while len(headings) >= level:
                headings.pop()
            headings.append(heading_text)
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            flush_paragraph()
            content = bullet_match.group(1).strip()
            prefix = context_prefix()
            entries.append(f"{prefix}: {content}" if prefix else content)
            continue

        if not stripped:
            flush_paragraph()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            continue

        paragraph_lines.append(stripped)

    flush_paragraph()

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        normalized = _normalize_text(entry)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(entry.strip())
    return deduped


def merge_entries(
    existing: Sequence[str], incoming: Sequence[str], limit: int
) -> tuple[list[str], dict[str, int], list[str]]:
    """Dedupe ``incoming`` against ``existing``, append until the joined store (entries joined by
    ``ENTRY_DELIMITER``) would exceed ``limit`` characters, and return ``(merged, stats,
    overflowed)`` -- overflowed entries in source order, nothing lost. Ported from the Hermes
    migration tool's function of the same name."""
    merged = list(existing)
    seen = {_normalize_text(entry) for entry in existing if entry.strip()}
    stats = {"existing": len(existing), "added": 0, "duplicates": 0, "overflowed": 0}
    overflowed: list[str] = []

    current_len = len(ENTRY_DELIMITER.join(merged)) if merged else 0

    for entry in incoming:
        normalized = _normalize_text(entry)
        if not normalized:
            continue
        if normalized in seen:
            stats["duplicates"] += 1
            continue

        candidate_len = len(entry) if not merged else current_len + len(ENTRY_DELIMITER) + len(entry)
        if candidate_len > limit:
            stats["overflowed"] += 1
            overflowed.append(entry)
            continue

        merged.append(entry)
        seen.add(normalized)
        current_len = candidate_len
        stats["added"] += 1

    return merged, stats, overflowed


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _store_bytes(entries: list[str]) -> bytes:
    return (ENTRY_DELIMITER.join(entries) + "\n").encode("utf-8")


def _overflow_bytes(entries: list[str]) -> bytes:
    return ("\n".join(entries) + "\n").encode("utf-8")


def build_memory_files(agent_dir: Path) -> dict[str, bytes]:
    """Build the profile-relative ``memories/`` files for one agent's Hermes profile, from an
    OpenClaw agent workspace directory (``workspace-{agentId}/``).

    - ``MEMORY.md`` entries followed by every ``memory/*.md`` daily note (sorted by filename,
      same order the Hermes migration tool's own ``daily-memory`` step reads them) are merged
      into one destination store, ``memories/MEMORY.md`` -- a single ``merge_entries`` call over
      the concatenated incoming list is equivalent to Hermes's own two sequential merges (one
      destination read between them), since ``merge_entries`` is a pure left-to-right accumulator.
    - ``USER.md`` entries merge separately into ``memories/USER.md``.
    - Anything that would overflow its store's char limit is written to a human-readable sidecar
      (``memories/MEMORY.overflow.md`` / ``memories/USER.overflow.md``, one entry per line) instead
      of being dropped. Hermes never reads the sidecar.
    - A store (or its overflow) is present in the result only when it has content -- a workspace
      with none of ``MEMORY.md``/``memory/*.md``/``USER.md`` returns ``{}``.
    """
    out: dict[str, bytes] = {}

    memory_incoming: list[str] = []
    memory_md = agent_dir / "MEMORY.md"
    if memory_md.is_file():
        memory_incoming.extend(extract_markdown_entries(_read_text(memory_md)))
    daily_dir = agent_dir / "memory"
    if daily_dir.is_dir():
        for daily_file in sorted(p for p in daily_dir.iterdir() if p.is_file() and p.suffix == ".md"):
            memory_incoming.extend(extract_markdown_entries(_read_text(daily_file)))
    if memory_incoming:
        merged, _stats, overflow = merge_entries([], memory_incoming, MEMORY_CHAR_LIMIT)
        if merged:
            out["memories/MEMORY.md"] = _store_bytes(merged)
        if overflow:
            out["memories/MEMORY.overflow.md"] = _overflow_bytes(overflow)

    user_md = agent_dir / "USER.md"
    if user_md.is_file():
        user_incoming = extract_markdown_entries(_read_text(user_md))
        if user_incoming:
            merged_u, _stats_u, overflow_u = merge_entries([], user_incoming, USER_CHAR_LIMIT)
            if merged_u:
                out["memories/USER.md"] = _store_bytes(merged_u)
            if overflow_u:
                out["memories/USER.overflow.md"] = _overflow_bytes(overflow_u)

    return out
