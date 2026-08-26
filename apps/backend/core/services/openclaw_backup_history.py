"""Carry an imported agent's conversation history into its Isol8 container.

Inputs (from the backup, see openclaw_backup_import._history_for):
  agents/<old>/sessions/{sessions.json,*.jsonl} — the canonical append-only
      transcript; PREFERRED. The gateway rebuilds a fresh pinned-schema DB from
      it on startup (doctor JSON->SQLite migration), which avoids the
      cross-version SQLite migration whose compact contends with litestream and
      crash-loops the container (#2038).
  agents/<old>/agent/openclaw-agent.sqlite   — a derived store; FALLBACK, used
      only when the backup carries no JSON log (auth profiles → ALWAYS deleted
      from it here).

Output: ``<efs user root>/import-state/agents/<new-uuid>/{agent/,sessions/}``.
The container's local-sqlite boot script (step 4b) moves that into its LOCAL
``agents/<new-uuid>/`` after the Litestream restores, so the import wins over
the empty replica the freshly registered agent produced before the bounce.

Sanitization is stdlib sqlite3 on a COPY: auth_profile_* and state_leases
emptied, schema_meta.agent_id re-pointed (the gateway asserts ownership on
open — ``assertExistingSchemaOwner``), ``agent:<old>:`` → ``agent:<new>:`` in
every session-key column and inside entry_json, FTS/derived tables dropped so
they rebuild. Every statement tolerates a missing table: the source may be
older or newer than the pinned schema and the gateway migrates forward on open.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from core.containers import get_workspace
from core.services.openclaw_backup_import import HistoryInfo

_DERIVED_TABLES = (
    "session_transcript_fts",
    "session_transcript_fts_config",
    "session_transcript_fts_content",
    "session_transcript_fts_data",
    "session_transcript_fts_docsize",
    "session_transcript_fts_idx",
    "session_transcript_index_state",
)
_KEY_COLUMNS = (
    ("sessions", "session_key"),
    ("sessions", "parent_session_key"),
    ("session_entries", "session_key"),
    ("session_routes", "session_key"),
)


def _chown_tree(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for n in (*dirs, *files):
            try:
                os.chown(os.path.join(root, n), 1000, 1000)
            except OSError:
                pass
    try:
        os.chown(path, 1000, 1000)
    except OSError:
        pass


def _exec(db: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
    try:
        db.execute(sql, params)
    except sqlite3.OperationalError as exc:
        # Tolerate ONLY "this schema doesn't have that table/column" — the
        # documented reason every statement here is wrapped. Anything else
        # (a locked db, a genuinely malformed statement) must not be
        # silently swallowed; it means something is wrong with the source
        # file, not just a schema-version mismatch.
        msg = str(exc)
        if "no such table" in msg or "no such column" in msg:
            return
        raise


def sanitize_agent_db(src: Path, dst: Path, *, old_id: str, new_id: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    for sidecar in ("-wal", "-shm", "-journal"):
        Path(str(dst) + sidecar).unlink(missing_ok=True)
    db = sqlite3.connect(dst)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        _exec(db, "DELETE FROM auth_profile_store")
        _exec(db, "DELETE FROM auth_profile_state")
        _exec(db, "DELETE FROM state_leases")
        _exec(db, "UPDATE schema_meta SET agent_id = ? WHERE meta_key = 'primary'", (new_id,))
        old_prefix, new_prefix = f"agent:{old_id}:", f"agent:{new_id}:"
        for table, column in _KEY_COLUMNS:
            _exec(
                db,
                f"UPDATE {table} SET {column} = replace({column}, ?, ?) WHERE {column} LIKE ?",
                (old_prefix, new_prefix, old_prefix + "%"),
            )
        _exec(
            db,
            "UPDATE session_entries SET entry_json = replace(entry_json, ?, ?)",
            (f'"{old_prefix}', f'"{new_prefix}'),
        )
        for t in _DERIVED_TABLES:
            _exec(db, f"DROP TABLE IF EXISTS {t}")
        db.commit()
        db.execute("VACUUM")
    finally:
        db.close()


def rewrite_legacy_sessions(src_dir: Path, dst_dir: Path, *, old_id: str, new_id: str) -> None:
    old_prefix, new_prefix = f"agent:{old_id}:", f"agent:{new_id}:"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for p in src_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_dir)
        out = dst_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix in (".json", ".jsonl"):
            out.write_text(
                p.read_text(encoding="utf-8", errors="replace").replace(old_prefix, new_prefix), encoding="utf-8"
            )
        else:
            shutil.copyfile(p, out)


def stage_history(*, owner_id: str, source_agent_id: str, new_agent_id: str, history: HistoryInfo) -> list[str]:
    import_state = get_workspace().user_path(owner_id) / "import-state"
    # Build under a hidden `.tmp-<id>` sibling of agents/, then atomically
    # rename into place. The boot script's hand-off (isol8-local-sqlite-boot.sh
    # step 4b) only globs import-state/agents/*, so a `.tmp-*` dir left behind
    # by a crash mid-build is never partially consumed — os.replace is what
    # makes the final agents/<id> either fully old or fully new, never a
    # half-written directory the boot script could pick up.
    tmp = import_state / f".tmp-{new_agent_id}"
    final = import_state / "agents" / new_agent_id
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    # Prefer the legacy JSON session log over the raw agent SQLite. OpenClaw's
    # sessions/*.jsonl is the canonical append-only transcript; the SQLite is a
    # derived store the gateway rebuilds from it (the doctor JSON->SQLite
    # migration). Handing off an older-version backup's raw SQLite makes the
    # pinned gateway run a cross-version schema migration whose compact/checkpoint
    # contends with the litestream supervisor ("SQLite checkpoint remained busy")
    # and crash-loops the container (#2038). Handing off only the JSON log puts
    # imported history on the same fresh-DB-under-litestream path every
    # marketplace hire already uses. Lossless: a real 2026.3.11 backup's agent
    # SQLite held zero session rows while its JSON log held the entire transcript.
    # The raw-SQLite path stays as the fallback for a backup that has no JSON log.
    if history.sessions_dir is not None:
        rewrite_legacy_sessions(history.sessions_dir, tmp / "sessions", old_id=source_agent_id, new_id=new_agent_id)
    elif history.sqlite is not None:
        sanitize_agent_db(
            history.sqlite, tmp / "agent" / "openclaw-agent.sqlite", old_id=source_agent_id, new_id=new_agent_id
        )
    _chown_tree(tmp)
    final.parent.mkdir(parents=True, exist_ok=True)
    # The backend writes EFS through its NON-squashed root mount, so import-state/
    # and import-state/agents/ are created root-owned. The container reads EFS
    # through a uid-1000-SQUASHED access point: it can delete the 1000-owned
    # <uuid> tree but NOT unlink it from a root-owned parent dir. Without chowning
    # the parents, the boot script's cleanup `rm` of agents/<uuid> fails
    # Permission denied and, under `set -e`, crash-loops the container. Chown the
    # two parents to 1000 too so the cleanup can succeed. (The boot script also
    # guards the rm, but this keeps the hand-off from lingering on EFS.)
    for _parent in (import_state, final.parent):
        try:
            os.chown(_parent, 1000, 1000)
        except OSError:
            pass
    if final.exists():
        shutil.rmtree(final)
    os.replace(tmp, final)
    return [
        f"conversation history for {source_agent_id!r} is staged; it is applied the next time your "
        "workspace starts on an updated platform image"
    ]
