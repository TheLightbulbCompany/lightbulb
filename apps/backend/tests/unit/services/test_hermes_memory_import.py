"""M6-R47 -- the ported Hermes memory-entry extractor/merger.

Ground truth is Hermes's own OpenClaw migration tool (``openclaw_to_hermes.py``, tag
v2026.9.14) -- these tests pin the ported behaviour, not a from-scratch spec.
"""

from __future__ import annotations

from core.services.hermes_memory_import import (
    ENTRY_DELIMITER,
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    build_memory_files,
    extract_markdown_entries,
    merge_entries,
)

# ---------------------------------------------------------------------------
# extract_markdown_entries
# ---------------------------------------------------------------------------


def test_headings_become_prefixes():
    text = "# Projects\n## Alpha\nShip the migration job.\n"
    assert extract_markdown_entries(text) == ["Projects > Alpha: Ship the migration job."]


def test_bullets_split_into_separate_entries():
    text = "## Notes\n- first thing\n- second thing\n"
    assert extract_markdown_entries(text) == ["Notes: first thing", "Notes: second thing"]


def test_fenced_code_block_is_skipped():
    text = "## Notes\nBefore.\n```\nnot a memory entry\nstill not one\n```\nAfter.\n"
    entries = extract_markdown_entries(text)
    assert entries == ["Notes: Before.", "Notes: After."]
    assert not any("not a memory entry" in e for e in entries)


def test_table_row_is_skipped():
    text = "## Notes\n| a | b |\n|---|---|\n| 1 | 2 |\nReal entry.\n"
    entries = extract_markdown_entries(text)
    assert entries == ["Notes: Real entry."]


def test_empty_file_yields_no_entries():
    assert extract_markdown_entries("") == []
    assert extract_markdown_entries("   \n\n  ") == []


def test_heading_that_is_just_a_source_filename_is_dropped_from_the_prefix():
    text = "# MEMORY.md\n## Real heading\nContent line.\n"
    assert extract_markdown_entries(text) == ["Real heading: Content line."]


# ---------------------------------------------------------------------------
# merge_entries
# ---------------------------------------------------------------------------


def test_duplicate_entries_are_dropped():
    merged, stats, overflow = merge_entries(["Existing entry"], ["Existing entry", "New entry"], limit=1000)
    assert merged == ["Existing entry", "New entry"]
    assert stats["duplicates"] == 1
    assert stats["added"] == 1
    assert overflow == []


def test_duplicate_detection_is_whitespace_insensitive():
    merged, stats, _overflow = merge_entries(["Line one"], ["Line   one"], limit=1000)
    assert merged == ["Line one"]
    assert stats["duplicates"] == 1


def test_store_never_exceeds_the_limit():
    incoming = [f"entry number {i} of a certain length to force overflow soon" for i in range(20)]
    limit = 200
    merged, _stats, overflow = merge_entries([], incoming, limit)
    assert len(ENTRY_DELIMITER.join(merged)) <= limit
    assert overflow  # the fixture is sized to guarantee at least one overflow


def test_overflow_returned_in_source_order():
    # Each entry alone fits; force the running total over budget partway through so the tail
    # entries overflow, and confirm they come back in the same order they were given.
    incoming = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
    limit = 90  # room for exactly the first two entries plus one delimiter
    merged, _stats, overflow = merge_entries([], incoming, limit)
    assert merged == ["a" * 40, "b" * 40]
    assert overflow == ["c" * 40, "d" * 40]


# ---------------------------------------------------------------------------
# build_memory_files
# ---------------------------------------------------------------------------


def _write(agent_dir, rel_path, content):
    path = agent_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_build_memory_files_merges_memory_and_daily_notes_and_user(tmp_path):
    agent_dir = tmp_path / "workspace-agent1"
    _write(agent_dir, "MEMORY.md", "## Notes\nFrom MEMORY.md.\n")
    _write(agent_dir, "memory/2026-09-01.md", "## Day one\nEarlier daily note.\n")
    _write(agent_dir, "memory/2026-09-02.md", "## Day two\nLater daily note.\n")
    _write(agent_dir, "USER.md", "## Profile\nPrefers concise replies.\n")

    files = build_memory_files(agent_dir)

    assert set(files) == {"memories/MEMORY.md", "memories/USER.md"}
    memory_text = files["memories/MEMORY.md"].decode("utf-8")
    assert memory_text.endswith("\n")
    entries = memory_text[:-1].split(ENTRY_DELIMITER)
    assert entries == ["Notes: From MEMORY.md.", "Day one: Earlier daily note.", "Day two: Later daily note."]

    user_text = files["memories/USER.md"].decode("utf-8")
    assert user_text == "Profile: Prefers concise replies.\n"


def test_build_memory_files_with_no_sources_returns_empty_dict(tmp_path):
    agent_dir = tmp_path / "workspace-agent1"
    agent_dir.mkdir(parents=True)
    assert build_memory_files(agent_dir) == {}


def test_build_memory_files_oversize_memory_overflows_to_sidecar(tmp_path):
    agent_dir = tmp_path / "workspace-agent1"
    lines = "\n".join(f"- distinct memory entry number {i} with enough text to add up fast" for i in range(40))
    _write(agent_dir, "MEMORY.md", f"## Notes\n{lines}\n")

    files = build_memory_files(agent_dir)

    assert "memories/MEMORY.md" in files
    assert "memories/MEMORY.overflow.md" in files
    # The budget is in characters (model-independent, per Hermes's own memory_tool_store.py
    # docstring), not UTF-8 bytes -- decode before measuring, since ENTRY_DELIMITER's "§"
    # is one character but two bytes.
    assert len(files["memories/MEMORY.md"].decode("utf-8")[:-1]) <= MEMORY_CHAR_LIMIT

    merged_entries = set(files["memories/MEMORY.md"].decode("utf-8")[:-1].split(ENTRY_DELIMITER))
    overflow_entries = set(files["memories/MEMORY.overflow.md"].decode("utf-8").splitlines())

    source_entries = extract_markdown_entries((agent_dir / "MEMORY.md").read_text())
    assert source_entries, "fixture must actually produce entries"
    for entry in source_entries:
        assert (entry in merged_entries) != (entry in overflow_entries), entry
    assert not (merged_entries & overflow_entries)


def test_build_memory_files_only_soul_or_agents_present_returns_empty_dict(tmp_path):
    # AGENTS.md/SOUL.md are handled elsewhere in the migration plan -- this module owns only
    # MEMORY.md/memory/**/USER.md, so their presence alone must not produce any output.
    agent_dir = tmp_path / "workspace-agent1"
    _write(agent_dir, "AGENTS.md", "playbook")
    _write(agent_dir, "SOUL.md", "# Ada\n")
    assert build_memory_files(agent_dir) == {}


def test_user_char_limit_is_smaller_than_memory_char_limit():
    # Sanity guard against a copy/paste swap of the two ported constants.
    assert USER_CHAR_LIMIT < MEMORY_CHAR_LIMIT
