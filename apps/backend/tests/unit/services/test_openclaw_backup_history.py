import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import workspace as workspace_module  # noqa: E402
from core.services import openclaw_backup_history as bh  # noqa: E402
from core.services.openclaw_backup_import import HistoryInfo  # noqa: E402


def _agent_db(path: Path) -> None:
    """Minimal per-agent DB shaped like the pinned tag's schema (schema_meta,
    sessions, session_entries, session_routes, auth_profile_*)."""
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE schema_meta (meta_key TEXT PRIMARY KEY, role TEXT, schema_version INTEGER, agent_id TEXT, app_version TEXT, created_at INTEGER, updated_at INTEGER);
      INSERT INTO schema_meta VALUES ('primary','agent',7,'main','2026.3.11',1,1);
      CREATE TABLE sessions (session_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, parent_session_key TEXT, created_at INTEGER, updated_at INTEGER);
      INSERT INTO sessions VALUES ('s1','agent:main:main',NULL,1,1);
      INSERT INTO sessions VALUES ('s2','agent:main:telegram:123','agent:main:main',1,1);
      CREATE TABLE session_entries (session_id TEXT, session_key TEXT, entry_json TEXT, status TEXT, updated_at INTEGER);
      INSERT INTO session_entries VALUES ('s1','agent:main:main','{"sessionKey":"agent:main:main","x":1}',NULL,1);
      CREATE TABLE session_routes (session_key TEXT PRIMARY KEY, session_id TEXT, updated_at INTEGER);
      INSERT INTO session_routes VALUES ('agent:main:main','s1',1);
      CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY, payload TEXT);
      INSERT INTO auth_profile_store VALUES ('primary','{"profiles":{"anthropic":{"token":"sk-secret"}}}');
      CREATE TABLE auth_profile_state (state_key TEXT PRIMARY KEY, payload TEXT);
      INSERT INTO auth_profile_state VALUES ('primary','{}');
      CREATE TABLE state_leases (lease_key TEXT PRIMARY KEY, owner TEXT);
      INSERT INTO state_leases VALUES ('gateway','old-host');
    """)
    db.commit()
    db.close()


def test_sanitize_agent_db_strips_auth_and_repoints_agent(tmp_path: Path):
    src = tmp_path / "in.sqlite"
    _agent_db(src)
    dst = tmp_path / "out.sqlite"
    bh.sanitize_agent_db(src, dst, old_id="main", new_id="7a1b-new")
    db = sqlite3.connect(dst)
    assert db.execute("SELECT agent_id FROM schema_meta WHERE meta_key='primary'").fetchone()[0] == "7a1b-new"
    assert db.execute("SELECT count(*) FROM auth_profile_store").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM auth_profile_state").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM state_leases").fetchone()[0] == 0
    keys = sorted(r[0] for r in db.execute("SELECT session_key FROM sessions"))
    assert keys == ["agent:7a1b-new:main", "agent:7a1b-new:telegram:123"]
    assert (
        db.execute("SELECT parent_session_key FROM sessions WHERE session_id='s2'").fetchone()[0]
        == "agent:7a1b-new:main"
    )
    assert db.execute("SELECT session_key FROM session_routes").fetchone()[0] == "agent:7a1b-new:main"
    entry = json.loads(db.execute("SELECT entry_json FROM session_entries").fetchone()[0])
    assert entry["sessionKey"] == "agent:7a1b-new:main"
    # source untouched
    assert sqlite3.connect(src).execute("SELECT count(*) FROM auth_profile_store").fetchone()[0] == 1


def test_sanitize_tolerates_missing_tables(tmp_path: Path):
    src = tmp_path / "in.sqlite"
    db = sqlite3.connect(src)
    db.executescript(
        "CREATE TABLE schema_meta (meta_key TEXT, agent_id TEXT); INSERT INTO schema_meta VALUES ('primary','main');"
    )
    db.commit()
    db.close()
    bh.sanitize_agent_db(src, tmp_path / "out.sqlite", old_id="main", new_id="n")
    assert sqlite3.connect(tmp_path / "out.sqlite").execute("SELECT agent_id FROM schema_meta").fetchone()[0] == "n"


def test_rewrite_legacy_sessions(tmp_path: Path):
    src = tmp_path / "sessions"
    src.mkdir()
    (src / "sessions.json").write_text(json.dumps({"agent:main:main": {"sessionId": "s1"}}))
    (src / "s1.jsonl").write_text('{"type":"session","sessionKey":"agent:main:main"}\n{"type":"message","text":"hi"}\n')
    dst = tmp_path / "out"
    bh.rewrite_legacy_sessions(src, dst, old_id="main", new_id="n")
    assert list(json.loads((dst / "sessions.json").read_text())) == ["agent:n:main"]
    assert '"sessionKey":"agent:n:main"' in (dst / "s1.jsonl").read_text()


def test_stage_history_writes_handoff_under_efs_root(tmp_path: Path):
    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    src_db = tmp_path / "in.sqlite"
    _agent_db(src_db)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text("{}")
    with patch.object(bh, "get_workspace", return_value=fake_ws), patch.object(bh, "_chown_tree"):
        warnings = bh.stage_history(
            owner_id="owner_1",
            source_agent_id="main",
            new_agent_id="new-uuid",
            history=HistoryInfo(sqlite=src_db, sessions_dir=sessions, bytes=1),
        )
    handoff = tmp_path / "users" / "owner_1" / "import-state" / "agents" / "new-uuid"
    assert (handoff / "agent" / "openclaw-agent.sqlite").is_file()
    assert (handoff / "sessions" / "sessions.json").is_file()
    assert any("updated platform image" in w for w in warnings)
