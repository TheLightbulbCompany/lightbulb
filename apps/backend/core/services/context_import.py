"""Merge a pasted business context into one agent's Hermes user-profile memory.

The destination is ``memories/USER.md`` in the agent's S3 profile -- Hermes's own user-profile
store (``tools/memory_tool.py::get_memory_dir``), which the agent reads every turn and keeps
maintaining itself. NOT the profile-root ``USER.md`` (the pre-Hermes location Hermes never
reads) and NOT ``memories/MEMORY.md`` (the agent's own notes).

The entry extraction and merge are Hermes's own OpenClaw-migration semantics
(``optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py`` at v2026.9.14:
``extract_markdown_entries`` / ``parse_existing_memory_entries`` / ``merge_entries``). That file
used to be vendored and driven by ``hermes_memory_migration``; both went with the migration job
in #2786, so the three functions are ported here. Two deliberate differences from upstream, both
because a paste has no second copy to fall back on: table rows and fenced code are KEPT (upstream
drops them), and the overflow file is APPENDED to (upstream rewrites it per run), so a second
paste never loses the first one's spill.

Budget: ``USER_CHAR_LIMIT`` (the number the profile's config.yaml gives Hermes). Whatever does
not fit is spilled to ``memories/user_profile_overflow.txt`` -- upstream's own overflow name for
the ``user-profile`` option -- and one pointer entry in ``USER.md`` tells the agent it is there,
so a 40 KB paste neither blows the profile nor silently vanishes.

Delivery is ``write_profile_subtree``: content first, ``.revision`` last, and the bridge re-pulls
every object S3 touched since its last look on the agent's next turn (bridge ``sync.py``,
M6-R51). Concurrency is the bridge's last-writer-wins: an agent turn that rewrites ``USER.md``
at the same moment can win the race; a re-paste is the recovery.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

from core.config import settings
from core.services.hermes_profiles import USER_CHAR_LIMIT, fetch_profile_subtree, write_profile_subtree

ENTRY_DELIMITER = "\n§\n"
USER_MD = "memories/USER.md"
OVERFLOW = "memories/user_profile_overflow.txt"
#: Dotfile, so the Files panel and every owner-facing route hide it (``is_hidden_profile_path``).
#: It is the "context imported" signal: USER.md alone cannot say who wrote an entry.
MARKER = ".context-import"
OVERFLOW_POINTER = (
    "More of the owner's pasted business context is in "
    "$HERMES_HOME/memories/user_profile_overflow.txt; read it when it is relevant."
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$")
_TABLE_RULE_RE = re.compile(r"^\|[\s:|-]+\|$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_entries(text: str) -> list[str]:
    """Upstream ``extract_markdown_entries``: one entry per bullet / paragraph, prefixed with its
    heading path; deduped. Table rows are one entry each and code lines join their paragraph."""
    entries: list[str] = []
    headings: list[str] = []
    para: list[str] = []

    def prefix() -> str:
        return " > ".join(
            h for h in headings if h and not re.search(r"\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY)\.md\b", h, re.I)
        )

    def add(content: str) -> None:
        p = prefix()
        entries.append(f"{p}: {content}" if p else content)

    def flush() -> None:
        nonlocal para
        block = " ".join(para).strip()
        para = []
        if block:
            add(block)

    in_code = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code:
            if stripped:
                para.append(stripped)
            continue
        if m := _HEADING_RE.match(stripped):
            flush()
            level = len(m.group(1))
            del headings[level - 1 :]
            headings.append(m.group(2).strip())
            continue
        if m := _BULLET_RE.match(raw.rstrip()):
            flush()
            add(m.group(1).strip())
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush()
            if not _TABLE_RULE_RE.match(stripped):
                add(stripped)
            continue
        para.append(stripped)
    flush()

    out: list[str] = []
    seen: set[str] = set()
    for e in entries:
        n = _norm(e)
        if n and n not in seen:
            seen.add(n)
            out.append(e.strip())
    return out


def _merge(existing: list[str], incoming: list[str], limit: int) -> tuple[list[str], list[str], list[str]]:
    """Upstream ``merge_entries``: (merged, added, overflowed)."""
    merged = list(existing)
    seen = {_norm(e) for e in existing}
    added: list[str] = []
    overflowed: list[str] = []
    length = len(ENTRY_DELIMITER.join(merged))
    for entry in incoming:
        n = _norm(entry)
        if not n or n in seen:
            continue
        seen.add(n)
        candidate = len(entry) if not merged else length + len(ENTRY_DELIMITER) + len(entry)
        if candidate > limit:
            overflowed.append(entry)
            continue
        merged.append(entry)
        added.append(entry)
        length = candidate
    return merged, added, overflowed


def _read(owner_id: str, agent_id: str, rels: tuple[str, ...]) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="context-import-") as tmp:
        root = Path(tmp)
        fetch_profile_subtree(owner_id, agent_id, rels, root)
        return {
            rel: (root / rel).read_text(encoding="utf-8", errors="replace") for rel in rels if (root / rel).is_file()
        }


def has_imported_context(owner_id: str, agent_id: str) -> bool:
    """Sync -- call via ``run_in_thread``."""
    return MARKER in _read(owner_id, agent_id, (MARKER,))


def merge_context(owner_id: str, agent_id: str, text: str) -> dict:
    """Merge ``text`` into the agent's ``memories/USER.md``; ``ValueError`` when it holds no
    entries. Returns ``{"merged": chars added to USER.md, "overflowed": bool}``. Sync -- call via
    ``run_in_thread``."""
    if not settings.OWNER_STATE_BUCKET:
        # write_profile_subtree no-ops on an empty bucket; refuse rather than report a save.
        raise RuntimeError("OWNER_STATE_BUCKET is not configured")
    incoming = extract_entries(text)
    if not incoming:
        raise ValueError("nothing to import")
    current = _read(owner_id, agent_id, (USER_MD, OVERFLOW))
    existing = [e.strip() for e in current.get(USER_MD, "").split(ENTRY_DELIMITER) if e.strip()]
    had_pointer = OVERFLOW_POINTER in existing
    existing = [e for e in existing if e != OVERFLOW_POINTER]
    old_spill = [line for line in current.get(OVERFLOW, "").splitlines() if line.strip()]
    spilled = {_norm(line) for line in old_spill}

    # The pointer's room is always reserved, so adding it never pushes USER.md over budget (unless
    # the agent's own entries already fill it -- theirs, not ours to evict).
    limit = USER_CHAR_LIMIT - len(ENTRY_DELIMITER) - len(OVERFLOW_POINTER)
    merged, added, overflowed = _merge(existing, [e for e in incoming if _norm(e) not in spilled], limit)
    # One line per entry in the spill file (upstream's format); a multi-line entry is flattened.
    new_spill = [_norm(e) for e in overflowed]
    if had_pointer or old_spill or new_spill:
        merged.append(OVERFLOW_POINTER)

    files = {USER_MD: (ENTRY_DELIMITER.join(merged) + "\n").encode(), MARKER: str(int(time.time())).encode()}
    if new_spill:
        files[OVERFLOW] = ("\n".join(old_spill + new_spill) + "\n").encode()
    write_profile_subtree(owner_id, agent_id, files, ())
    return {"merged": sum(len(e) for e in added), "overflowed": bool(new_spill)}
