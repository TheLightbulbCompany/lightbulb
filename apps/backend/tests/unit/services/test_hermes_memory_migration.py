"""M6-R47 -- wiring for driving Hermes's own vendored OpenClaw migration tool.

This tests only the WRAPPER (staging + running the real vendored ``Migrator`` + collecting its
output). It never re-tests the translator's own extraction/merge algorithm -- that is upstream
code, not ours (see ``apps/backend/vendor/hermes_openclaw_migration/README.md``).
"""

from __future__ import annotations

from core.services.hermes_memory_migration import build_memory_files
from vendor.hermes_openclaw_migration.openclaw_to_hermes import ENTRY_DELIMITER


def _write(agent_dir, rel_path, content):
    path = agent_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_build_memory_files_runs_the_real_translator(tmp_path):
    agent_dir = tmp_path / "workspace-agent1"
    _write(agent_dir, "MEMORY.md", "Remembered from MEMORY.md.")
    _write(agent_dir, "memory/2026-09-01.md", "Earlier daily note.")
    _write(agent_dir, "memory/2026-09-02.md", "Later daily note.")
    _write(agent_dir, "USER.md", "Prefers concise replies.")

    files = build_memory_files(agent_dir)

    assert set(files) == {"memories/MEMORY.md", "memories/USER.md"}

    memory_text = files["memories/MEMORY.md"].decode("utf-8")
    user_text = files["memories/USER.md"].decode("utf-8")

    # The translator's own delimiter, not a stand-in -- proves the real vendored tool ran.
    assert ENTRY_DELIMITER in memory_text

    for source_line in ("Remembered from MEMORY.md.", "Earlier daily note.", "Later daily note."):
        assert source_line in memory_text
    assert "Prefers concise replies." in user_text


def test_build_memory_files_with_no_sources_returns_empty_dict(tmp_path):
    agent_dir = tmp_path / "workspace-agent1"
    agent_dir.mkdir(parents=True)
    assert build_memory_files(agent_dir) == {}


def test_build_memory_files_only_soul_or_agents_present_returns_empty_dict(tmp_path):
    # AGENTS.md/SOUL.md are handled elsewhere in the migration plan -- this module owns only
    # MEMORY.md/memory/**/USER.md, so their presence alone must not run the translator at all.
    agent_dir = tmp_path / "workspace-agent1"
    _write(agent_dir, "AGENTS.md", "playbook")
    _write(agent_dir, "SOUL.md", "# Ada\n")
    assert build_memory_files(agent_dir) == {}
