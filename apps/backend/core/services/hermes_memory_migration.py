"""Drives Hermes's own OpenClaw->Hermes migration tool for the memory carry (Ruling M6-R47).

``scripts/migrate_workspace_to_hermes.py`` used to carry an OpenClaw agent's ``MEMORY.md`` and
``memory/`` daily-notes directory verbatim to profile locations Hermes never reads: its curated
memory lives at ``$HERMES_HOME/memories/MEMORY.md`` (agent notes) and
``$HERMES_HOME/memories/USER.md`` (user profile) -- see ``tools/memory_tool.py::get_memory_dir``
in the Hermes source. Rather than re-implementing Hermes's own entry-extraction/merge semantics
as a backend-owned port (which drifts the moment Hermes's tool changes), this module drives the
vendored tool itself (``vendor/hermes_openclaw_migration/`` -- see its README.md for provenance):
it stages a temp directory shaped like the OpenClaw home the tool's own ``source_candidate()``
expects, runs ONLY the three memory-related migration options, and returns whatever the tool
wrote as the profile-relative file map the migration job's carry plan expects.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from vendor.hermes_openclaw_migration.openclaw_to_hermes import Migrator

#: Only the memory-related options -- see ``MIGRATION_OPTION_METADATA`` in the vendored tool.
#: Every other option it supports (skills, channel settings, config.yaml sections, secrets, cron,
#: ...) is Hermes CLI functionality this job does not drive and must never select.
_MEMORY_OPTIONS = frozenset({"memory", "user-profile", "daily-memory"})

_DAILY_NOTES_SUBDIR = "memory"


def _stage_source(source_root: Path, agent_dir: Path) -> None:
    """Build the ``workspace/`` tree the vendored tool's ``source_candidate()`` reads
    (``workspace/MEMORY.md``, ``workspace/USER.md``, ``workspace/memory/*.md``), copied from an
    EFS agent workspace directory (``workspace-{agentId}/``)."""
    workspace = source_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("MEMORY.md", "USER.md"):
        src = agent_dir / name
        if src.is_file():
            shutil.copy2(src, workspace / name)
    daily_src = agent_dir / _DAILY_NOTES_SUBDIR
    if daily_src.is_dir():
        daily_dst = workspace / _DAILY_NOTES_SUBDIR
        daily_dst.mkdir(exist_ok=True)
        for f in daily_src.iterdir():
            if f.is_file() and f.suffix == ".md":
                shutil.copy2(f, daily_dst / f.name)


def build_memory_files(agent_dir: Path) -> dict[str, bytes]:
    """Run the vendored Hermes migration tool's memory options against one agent's OpenClaw
    workspace directory and return the profile-relative files it produced: ``memories/MEMORY.md``
    (merged ``MEMORY.md`` + every ``memory/*.md`` daily note, under Hermes's own char budget) and
    ``memories/USER.md`` (same, for ``USER.md``), plus the tool's own overflow sidecars
    (``write_overflow_entries``'s ``<option>_overflow.txt`` files) wherever something overflowed
    its store's budget -- never read by Hermes, kept so nothing is lost. Returns ``{}`` when the
    agent has none of ``MEMORY.md``/``memory/*.md``/``USER.md``.
    """
    if (
        not (agent_dir / "MEMORY.md").is_file()
        and not (agent_dir / "USER.md").is_file()
        and not (agent_dir / _DAILY_NOTES_SUBDIR).is_dir()
    ):
        return {}

    with tempfile.TemporaryDirectory(prefix="hermes-memory-migrate-") as tmp:
        tmp_path = Path(tmp)
        source_root = tmp_path / "source"
        target_root = tmp_path / "target"
        # Kept OUTSIDE target_root/memories/ so a walk of the real memory stores below can't
        # accidentally pick up the tool's own overflow sidecars as if they were a memory store.
        output_dir = tmp_path / "output"
        source_root.mkdir()
        target_root.mkdir()
        _stage_source(source_root, agent_dir)

        migrator = Migrator(
            source_root=source_root,
            target_root=target_root,
            execute=True,
            workspace_target=None,
            overwrite=True,
            migrate_secrets=False,
            output_dir=output_dir,
            selected_options=set(_MEMORY_OPTIONS),
        )
        migrator.migrate()

        out: dict[str, bytes] = {}
        memories_dir = target_root / "memories"
        if memories_dir.is_dir():
            for f in sorted(memories_dir.iterdir()):
                if f.is_file():
                    out[f"memories/{f.name}"] = f.read_bytes()

        overflow_dir = output_dir / "overflow"
        if overflow_dir.is_dir():
            for f in sorted(overflow_dir.iterdir()):
                if f.is_file():
                    out[f"memories/{f.name}"] = f.read_bytes()

        return out
