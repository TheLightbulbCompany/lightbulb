"""Phase 1 of Hermes's own session store: every turn records into the profile's state.db.

The fakes stand in for hermes_state_registry / run_agent / agent.curator, which exist only in
the image. The Hermes behaviour each test leans on is cited in bridge/turn.py and bridge/sync.py.
"""
import shutil
import sqlite3
import sys
import types
from contextlib import nullcontext

import pytest

from bridge import sync, turn
from tests import factories as f

PAPERCLIP = {"agentId": "ceo", "runId": "run-pc", "sessionId": "paperclip:c:agent:ceo:issue:i",
             "paperclipApiUrl": "url", "companyId": "c"}
CALL = {"agentId": "ceo", "runId": "run-call", "callId": "call-1", "sessionId": "agent:ceo:lightbulb:call:call-1"}


class FakeDB:
    """The SessionDB surface the bridge calls."""

    def __init__(self, path, tips=None, users=None):
        self.path, self.tips, self.users, self.cleared = path, tips or {}, users or {}, []
        self.ended, self.maintained = [], []

    def resolve_resume_session_id(self, sid):
        return self.tips.get(sid, sid)

    def update_system_prompt(self, sid, prompt):
        self.cleared.append((sid, prompt))

    def _read_one(self, sql, params):
        assert "COUNT(*)" in sql and "role = 'user'" in sql and "active = 1" in sql
        return (self.users.get(params[0], 0),)

    def end_session(self, sid, reason):
        self.ended.append((sid, reason))

    def maybe_auto_prune_and_vacuum(self, retention_days):
        self.maintained.append(retention_days)
        return {"pruned": 0}


#: What the fake AIAgent's next run_conversation calls return (or raise), in order; then "ok".
SCRIPT: list = []


@pytest.fixture
def hermes(monkeypatch):
    """Fake registry + AIAgent. Returns (acquired paths, built agents)."""
    acquired, built = [], []
    dbs = {}

    def acquire(path):
        acquired.append(path)
        return dbs.setdefault(path, FakeDB(path))

    class AIAgent:
        _memory_nudge_interval = 10
        _turns_since_memory = 0
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0
        model = "m"

        def __init__(self, **kw):
            self.kw, self.session_id, self.calls = kw, kw["session_id"], []
            self._session_db, self._persist_disabled = kw["session_db"], False
            built.append(self)

        def run_conversation(self, **kw):
            self.calls.append((kw, self._session_db, self._persist_disabled))
            nxt = SCRIPT.pop(0) if SCRIPT else {"api_calls": 1, "final_response": "ok"}
            if isinstance(nxt, BaseException):
                raise nxt
            if callable(nxt):
                return nxt(self)
            return nxt

    monkeypatch.setitem(sys.modules, "hermes_state_registry", types.SimpleNamespace(acquire=acquire))
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    monkeypatch.setattr(turn, "_session_dbs", {})
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda _a: {})
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (None, None))
    monkeypatch.setattr(turn, "_capture_api_errors", lambda _a: None)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    monkeypatch.setattr(turn, "paperclip_run_aborted", lambda _r: False)
    SCRIPT.clear()
    return acquired, built, dbs


def _run_room():
    body = f.ingress(recentHistory=[f.message(text="earlier", author={"kind": "human", "id": "user_1"})])
    assert turn._run_hermes("ceo", body, None)[0] == "completed"
    return body


def _run_call():
    turn.register_call_run(CALL)
    frames = turn.CallFramePublisher("", "", "call-1", "ceo", "run-call")
    assert turn._run_call(CALL, {"input": "hi", "instructions": "x"}, frames)[0] == "completed"


def _run_paperclip():
    assert turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[0] == "completed"


@pytest.mark.parametrize("run, key", [
    (_run_room, turn.lightbulb_session_key("ceo", f.ROOM)),
    (_run_call, CALL["sessionId"]),
    (_run_paperclip, PAPERCLIP["sessionId"]),
])
def test_every_turn_kind_gets_the_profiles_session_db_under_its_own_key(hermes, home, run, key):
    acquired, built, dbs = hermes
    run()
    (agent,) = built
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    assert agent.kw["session_db"] is db
    assert agent.kw["session_id"] == key and agent.kw["gateway_session_key"] == key


def test_one_shared_handle_per_profile_for_the_process(hermes, home):
    acquired, built, _dbs = hermes
    _run_paperclip()
    _run_paperclip()
    assert len(acquired) == 1 and built[0].kw["session_db"] is built[1].kw["session_db"]


def test_aiagent_is_built_in_exactly_one_place():
    """So the session_db wiring in _construct_agent covers every turn kind, present and future."""
    import pathlib
    import re
    root = pathlib.Path(turn.__file__).parents[1]
    calls = [p for d in ("bridge", "plugins") for p in (root / d).rglob("*.py")
             for _ in re.finditer(r"=\s*AIAgent\(", p.read_text())]
    assert calls == [root / "bridge" / "turn.py"]


def test_a_compressed_session_continues_on_its_tip(hermes, home):
    _acquired, built, dbs = hermes
    key = PAPERCLIP["sessionId"]
    dbs[sync._profile_dir("ceo") / "state.db"] = FakeDB(None, tips={key: key + "-child"})
    _run_paperclip()
    assert built[0].kw["session_id"] == key + "-child"
    assert built[0].kw["gateway_session_key"] == key  # routing keeps the stable key


def test_room_history_is_passed_unchanged_and_the_stored_prompt_is_never_reused(hermes, home):
    """No duplication and no drift: the model sees exactly today's history and system prompt.
    Hermes skips history rows by identity when it flushes (session_persistence.py:187-210), and
    it rebuilds the prompt from our system_message only when no stored prompt is present."""
    _acquired, built, dbs = hermes
    body = _run_room()
    (agent,) = built
    ((call, _db, _off),) = agent.calls
    assert call["conversation_history"] == turn._history(body, "ceo", {})
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    assert db.cleared == [(agent.session_id, None)]


def test_turns_run_unrecorded_outside_the_hermes_image(monkeypatch, home):
    monkeypatch.setattr(turn, "_session_dbs", {})
    monkeypatch.delitem(sys.modules, "hermes_state_registry", raising=False)
    assert turn._session_db("ceo") is None


@pytest.mark.parametrize("users, expected", [(0, 0), (9, 9), (13, 3), (20, 0)])
def test_the_memory_review_counter_comes_from_the_recorded_transcript(hermes, home, users, expected):
    """A call or Paperclip turn passes no history, so Hermes never hydrated its counter and the
    review never fired. The recorded transcript carries the count -- one indexed COUNT, never the
    session's rows."""
    _acquired, built, dbs = hermes
    dbs[sync._profile_dir("ceo") / "state.db"] = FakeDB(None, users={PAPERCLIP["sessionId"]: users})
    _run_paperclip()
    assert built[0]._turns_since_memory == expected


# ---- a state.db failure never costs the turn -----------------------------------------------------
def _failed(cause):
    def run(agent):
        agent._last_persistence_error_cause = cause
        return {"api_calls": 1, "final_response": "", "failed": True,
                "turn_exit_reason": "session_persistence_failed", "messages": []}
    return run


def test_a_broken_state_db_reruns_the_turn_unrecorded_and_stops_recording(hermes, home):
    _acquired, built, _dbs = hermes
    SCRIPT.append(_failed("disk"))
    _run_paperclip()  # asserts "completed"
    (agent,) = built
    (_kw, first_db, _), (_kw2, second_db, persist_off) = agent.calls
    assert first_db is not None and second_db is None and persist_off is True
    _run_paperclip()  # the next turn of this process runs unrecorded, without a failed attempt first
    assert built[1].kw["session_db"] is None and len(built[1].calls) == 1


def test_a_transient_failure_reruns_the_turn_but_keeps_recording(hermes, home):
    _acquired, built, _dbs = hermes
    SCRIPT.append(_failed("locked"))
    _run_paperclip()
    _run_paperclip()
    assert built[1].kw["session_db"] is not None


def test_a_state_db_error_escaping_the_run_reruns_it_unrecorded(hermes, home):
    _acquired, built, _dbs = hermes
    SCRIPT.append(sqlite3.DatabaseError("database disk image is malformed"))
    _run_paperclip()
    assert [db is None for _kw, db, _ in built[0].calls] == [False, True]


def test_a_failure_after_a_tool_ran_is_not_rerun(hermes, home):
    """A re-run would repeat the tool's side effects; the turn reports its failure instead."""
    _acquired, built, _dbs = hermes

    def tool_ran(agent):
        result = _failed("disk")(agent)
        result["messages"] = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}]
        return result
    SCRIPT.append(tool_ran)
    assert turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[0] == "failed"
    assert len(built[0].calls) == 1


def test_an_interrupted_turn_is_an_error_never_a_reply(hermes, home):
    """Hermes's own stop (liveness watchdog, lease) can carry partial text or its own
    "Your message was not processed"; neither may reach the room."""
    SCRIPT.append({"api_calls": 1, "final_response": "half an answ", "interrupted": True})
    assert turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[:3] == (
        "failed", turn.INTERRUPTED, "")
    SCRIPT.append({"api_calls": 0, "final_response": "Your message was not processed.", "interrupted": True})
    assert turn._run_hermes("ceo", f.ingress(), None)[:3] == ("error", turn.INTERRUPTED, "")


# ---- retention -----------------------------------------------------------------------------------
def test_every_session_but_a_rooms_is_ended_after_its_turn(hermes, home):
    """Hermes prunes only ended sessions; a per-turn agent never closes, so none ever ended."""
    _acquired, built, dbs = hermes
    _run_paperclip()
    _run_call()
    _run_room()
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    assert db.ended == [(PAPERCLIP["sessionId"], "agent_close"), (CALL["sessionId"], "agent_close")]


def test_retention_runs_once_per_process_when_state_db_is_first_opened(hermes, home):
    _acquired, _built, dbs = hermes
    _run_paperclip()
    _run_paperclip()
    assert dbs[sync._profile_dir("ceo") / "state.db"].maintained == [turn.STATE_DB_RETENTION_DAYS]


# ---- leases --------------------------------------------------------------------------------------
def _state_db_with_leases(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT, "
                     "acquired_at REAL, expires_at REAL)")
        conn.execute("CREATE TABLE compression_locks (session_id TEXT PRIMARY KEY, holder TEXT, "
                     "acquired_at REAL, expires_at REAL)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO session_turn_leases VALUES ('room', 'pid=7:turn=x:platform=lightbulb', 0, 9e12)")
        conn.execute("INSERT INTO compression_locks VALUES ('room', 'pid=7', 0, 9e12)")
        conn.execute("INSERT INTO messages (body) VALUES ('kept')")


def _rows(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_cold_restore_drops_leases_a_mid_turn_snapshot_carried(home, monkeypatch, tmp_path):
    src = tmp_path / "s3-state.db"
    _state_db_with_leases(src)

    class S3:
        def get_paginator(self, _op):
            return types.SimpleNamespace(paginate=lambda **_k: [{"Contents": [
                {"Key": f"{sync.S3_PREFIX}/ceo/state.db", "ETag": '"e"'}]}])

        def download_file(self, _bucket, _key, target):
            shutil.copyfile(src, target)

    monkeypatch.setattr(sync, "S3_BUCKET", "bucket")
    monkeypatch.setattr(sync, "_s3", lambda: S3())
    monkeypatch.setattr(sync, "_emit_restore_metric", lambda *_a: None)
    sync._restored.discard("ceo")
    sync.restore_profile("ceo")
    db = sync._profile_dir("ceo") / "state.db"
    assert _rows(db, "session_turn_leases") == 0 and _rows(db, "compression_locks") == 0
    assert _rows(db, "messages") == 1  # the transcript itself is untouched


def test_clearing_tolerates_a_state_db_without_lease_tables(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER)")
    sync.clear_turn_leases(db)
    sync.clear_turn_leases(tmp_path / "missing.db")


# ---- persistence ---------------------------------------------------------------------------------
def test_state_db_is_saved_with_the_profile():
    assert not sync.is_excluded("state.db") and not sync.is_reproducible("state.db")
    # its WAL/SHM never go: save_profile snapshots through the backup API (test_sync.py)
    assert sync.is_excluded("state.db-wal") and sync.is_excluded("state.db-shm")


# ---- curator -------------------------------------------------------------------------------------
@pytest.fixture
def curator(monkeypatch):
    runs, saved = [], []
    state = {"last_run_at": "2026-01-01"}
    fake = types.SimpleNamespace(
        load_state=lambda: dict(state),
        should_run_now=lambda: True,
        run_curator_review=lambda synchronous: (runs.append(synchronous), state.update(last_run_at="now")),
    )
    monkeypatch.setitem(sys.modules, "agent", types.SimpleNamespace(curator=fake))
    monkeypatch.setitem(sys.modules, "agent.curator", fake)
    monkeypatch.setattr(turn, "RESTORED", {"ceo"})
    monkeypatch.setattr(turn, "save_profile", lambda a: saved.append(a))
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda _a: nullcontext())
    monkeypatch.setattr(turn, "_turns_admitted", 0)
    monkeypatch.setattr(turn, "_curated_at", 0)
    return runs, saved


def test_the_curator_runs_once_the_last_turn_ends_and_saves_what_it_changed(curator):
    runs, saved = curator
    turn._busy_add(1)
    turn._busy_add(1)
    turn._end_turn()
    assert runs == []  # another turn is still live
    turn._end_turn()
    assert runs == [True] and saved == ["ceo"]  # synchronous, then the profile is saved
    assert turn._busy == 0


def test_the_microvm_stays_busy_while_the_curator_runs(curator, monkeypatch):
    """A roll or recycle must not stop the session mid-pass: /ping and hold_turns read _busy."""
    seen = []
    monkeypatch.setattr(sys.modules["agent.curator"], "run_curator_review",
                        lambda synchronous: seen.append((turn._busy, turn.hold_turns())))
    turn._busy_add(1)
    turn._end_turn()
    assert seen == [(1, False)]


def test_the_curator_runs_at_most_once_per_idle_period(curator):
    runs, _saved = curator
    turn._busy_add(1)
    turn._end_turn()
    turn._curate_when_idle()  # still the same idle period: nothing new happened
    assert len(runs) == 1
    turn._busy_add(1)
    turn._end_turn()
    assert len(runs) == 2


def test_the_curator_never_runs_while_a_delegation_is_live(curator, monkeypatch):
    from bridge import followups
    runs, _saved = curator
    monkeypatch.setattr(followups, "busy", lambda: True)
    turn._busy_add(1)
    turn._end_turn()
    assert runs == []


def test_a_curator_that_is_not_due_saves_nothing(curator, monkeypatch):
    runs, saved = curator
    monkeypatch.setattr(sys.modules["agent.curator"], "should_run_now", lambda: False)
    turn._busy_add(1)
    turn._end_turn()
    assert runs == [] and saved == []


def _hermes_state_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, content TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s', NULL)")
        conn.execute("INSERT INTO messages (session_id, content) VALUES ('s', 'hi')")


def test_state_db_goes_up_only_when_its_transcript_moved(home, monkeypatch):
    """Whole-file snapshots, so a save whose state.db changed only Hermes's post-turn bookkeeping
    (a title, token counters) must not upload it again; a recorded turn or a prune must."""
    import os
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket")
    uploaded = []
    monkeypatch.setattr(sync, "_s3", lambda: types.SimpleNamespace(
        upload_file=lambda path, bucket, key: uploaded.append(key)))
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    db = profile / "state.db"
    _hermes_state_db(db)

    def touch_and_save(sql=None):
        if sql:
            with sqlite3.connect(db) as conn:
                conn.execute(sql)
        os.utime(db, ns=(1, time_ns[0]))
        time_ns[0] += 10**9
        uploaded.clear()
        sync.save_profile("ceo")
        return uploaded == [f"{sync.S3_PREFIX}/ceo/state.db"]

    time_ns = [10**18]
    assert touch_and_save()                                                      # first save of the process
    assert not touch_and_save("UPDATE sessions SET title = 't'")                 # bookkeeping only
    assert touch_and_save("INSERT INTO messages (session_id, content) VALUES ('s', 'x')")  # a turn
    assert touch_and_save("DELETE FROM sessions")                                # a prune


def test_a_sigterm_first_pass_leaves_sqlite_for_the_second(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket")
    uploaded = []
    monkeypatch.setattr(sync, "_s3", lambda: types.SimpleNamespace(
        upload_file=lambda path, bucket, key: uploaded.append(key.rsplit("/", 1)[1])))
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    _hermes_state_db(profile / "state.db")
    (profile / "notes.md").write_text("x")
    sync.save_profile("ceo", sqlite=False)
    assert uploaded == ["notes.md"]
    sync.save_profile("ceo")
    assert uploaded == ["notes.md", "state.db"]
