"""Hermes's own session store: every turn records into the profile's state.db, and a room turn
runs on its room session's own transcript.

The fakes stand in for hermes_state_registry / run_agent / agent.curator, which exist only in
the image. The Hermes behaviour each test leans on is cited in bridge/turn.py and bridge/sync.py.
"""
import json
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
CALL_ID, NEXT_CALL_ID = "ca11ca11-1111-4111-8111-111111111111", "ca11ca11-2222-4222-8222-222222222222"
CALL = {"agentId": "ceo", "runId": "run-call", "roomId": f.ROOM, "family": "voice", "callId": CALL_ID,
        "sessionId": turn.lightbulb_session_key("ceo", f.ROOM)}
CALL_KEY, NEXT_CALL_KEY = turn.call_session_key("ceo", CALL_ID), turn.call_session_key("ceo", NEXT_CALL_ID)


class FakeDB:
    """The SessionDB surface the bridge calls. ``transcripts`` holds each session's rows."""

    def __init__(self, path, tips=None, meta=None, transcripts=None):
        self.path, self.tips = path, tips or {}
        self.meta, self.transcripts = meta or {}, transcripts or {}
        self.ended, self.maintained, self.reads, self.prompts = [], [], [], []

    def resolve_resume_session_id(self, sid):
        return self.tips.get(sid, sid)

    def update_system_prompt(self, sid, prompt):
        self.prompts.append((sid, prompt))

    def get_messages_as_conversation(self, sid, repair_alternation=False):
        assert not repair_alternation  # observed rows are split out before Hermes's repair
        self.reads.append(sid)
        if isinstance(self.transcripts.get(sid), BaseException):
            raise self.transcripts[sid]
        return [dict(r) for r in self.transcripts.get(sid, [])]

    def has_platform_message_id(self, sid, pid):
        return any(r.get("message_id") == pid for r in self.transcripts.get(sid, []))

    def message_count(self, sid=None):
        return len(self.transcripts.get(sid, []))

    def append_message(self, session_id, role, content, platform_message_id=None, observed=False):
        row = {"role": role, "content": content, **({"message_id": platform_message_id} if platform_message_id else {})}
        self.transcripts.setdefault(session_id, []).append({**row, **({"observed": True} if observed else {})})

    def ensure_session(self, sid, source="unknown"):
        self.transcripts.setdefault(sid, [])

    def get_session(self, sid):
        return {"id": sid, "model_config": json.dumps(self.meta[sid])} if sid in self.meta else None

    def patch_session_model_config(self, sid, patch):
        merged = {**self.meta.get(sid, {}), **patch}
        self.meta[sid] = {k: v for k, v in merged.items() if v is not None}

    def end_session(self, sid, reason):
        self.ended.append((sid, reason))

    def maybe_auto_prune_and_vacuum(self, retention_days=90):
        self.maintained.append(retention_days)
        return {"pruned": 0}


class HandleCache:
    """gateway.session_db_recovery.RecoverableHandleCache, minus the backoff."""

    def __init__(self):
        self.handles = {}

    def get(self, path, opener):
        if path not in self.handles:
            self.handles[path] = opener()
        return self.handles[path]


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
        _turns_since_memory = _iters_since_skill = 0
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

        def steer(self, text):
            return True

        def _ensure_db_session(self):
            pass

    monkeypatch.setitem(sys.modules, "hermes_state_registry", types.SimpleNamespace(acquire=acquire))
    monkeypatch.setitem(sys.modules, "gateway", sys.modules.get("gateway") or types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_db_recovery", types.SimpleNamespace(RecoverableHandleCache=HandleCache))
    monkeypatch.setitem(sys.modules, "agent", sys.modules.get("agent") or types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.agent_runtime_helpers",
                        types.SimpleNamespace(repair_message_sequence=lambda _agent, _messages: 0))
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    monkeypatch.setattr(turn, "_session_dbs", None)
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


def _voice_request(text="Aden (voice): hi", history=()):
    voice = {"roomId": f.ROOM, "roomKind": "channel", "speaker": {"id": "user_9", "name": "Aden"},
             "utterance": text.split(": ", 1)[-1]}
    return {"input": text, "instructions": "x", "voice": voice,
            "recentHistory": list(history), "participants": f.ingress()["participants"]}


def _spoken(text):
    """What a spoken turn says: a typed turn's turn line (sender, live sessions), then the words."""
    return turn._spoken_turn_input({"sender": {"id": "user_9", "name": "Aden"}}, text)


def _run_call(text="Aden (voice): hi", history=(), run_id="run-call", call_id=CALL_ID, outcome="completed"):
    identity = {**CALL, "runId": run_id, "callId": call_id}
    turn.register_call_run(identity)
    frames = turn.VoiceFramePublisher("", f.ROOM, "ceo", run_id)
    assert turn._run_call(identity, _voice_request(text, history), frames)[0] == outcome


def _run_paperclip():
    assert turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[0] == "completed"


@pytest.mark.parametrize("run, key", [
    (_run_room, turn.lightbulb_session_key("ceo", f.ROOM)),
    (_run_call, CALL_KEY),
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


def test_a_room_keeps_hermes_stored_prompt_and_its_own_context_rides_as_ephemeral(hermes, home):
    """Hermes reuses a continuing session's stored prompt (prefix cache); the room's per-turn
    context is added at API time, never stored, as the gateway passes channel context."""
    _acquired, built, dbs = hermes
    _run_room()
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    assert db.prompts == [] and _said(built)["system_message"] is None
    assert built[0].ephemeral_system_prompt.endswith(turn._observe_prompt("ceo", f.ingress()))


def test_turns_run_unrecorded_outside_the_hermes_image(monkeypatch, home):
    monkeypatch.setattr(turn, "_session_dbs", None)
    monkeypatch.delitem(sys.modules, "hermes_state_registry", raising=False)
    assert turn._session_db("ceo") is None


# ---- a room turn runs on its own transcript ------------------------------------------------------
ROOM_KEY = turn.lightbulb_session_key("ceo", f.ROOM)
ME = {"kind": "agent", "id": "ceo", "displayName": "CEO"}


def _room_db(dbs, **kw):
    db = dbs[sync._profile_dir("ceo") / "state.db"] = FakeDB(None, **kw)
    return db


def _room_turn(*history, seq=10, **over):
    body = f.ingress(message=f.message(messageSeq=seq, text="now"), recentHistory=list(history), **over)
    assert turn._run_hermes("ceo", body, None)[0] == "completed"
    return body


def _said(built):
    (call, _db, _off), *_ = built[-1].calls
    return call


def _observed(*lines):
    return f"{turn.OBSERVED_HEADER}\n" + "\n".join(lines) + f"\n\n{turn.ADDRESSED_HEADER}\n"


ANA = {"kind": "human", "id": "user_2", "displayName": "Ana"}


def test_a_room_turn_runs_on_its_transcript_and_observes_what_it_does_not_hold(hermes, home):
    """The gateway's observed group context: unheld room messages become observed rows, kept out
    of the replayed history and handed over ahead of the addressed message. Its own room-turn
    reply is held (it answers a trigger the session recorded); its post from elsewhere is not."""
    _acquired, built, dbs = hermes
    db = _room_db(dbs, transcripts={ROOM_KEY: [{"role": "user", "content": "Prez: hi", "message_id": "m1"},
                                               {"role": "assistant", "content": "hello"}]})
    body = _room_turn(f.message(messageId="m1", messageSeq=1, text="hi"),
                      f.message(messageId="r1", messageSeq=2, author=ME, text="hello", replyToMessageId="m1"),
                      f.message(messageId="m2", messageSeq=3, author=ANA, text="hey"),
                      f.message(messageId="p1", messageSeq=4, author=ME, text="nightly digest"))
    call = _said(built)
    assert call["conversation_history"] == db.transcripts[ROOM_KEY][:2]  # observed rows stay out
    assert [r for r in db.transcripts[ROOM_KEY] if r.get("observed")] == [
        {"role": "user", "content": "[Ana|user_2]\nhey", "message_id": "m2", "observed": True},
        {"role": "user", "content": "[CEO|ceo]\nnightly digest", "message_id": "p1", "observed": True}]
    turn_text = call["persist_user_message"]  # the observed block is API-only, as in the gateway
    assert call["user_message"] == _observed("[Ana|user_2]\nhey", "[CEO|ceo]\nnightly digest") + turn_text
    assert f.split_turn_input(turn_text)[1] == "Prez: now"
    assert call["persist_user_platform_id"] == body["message"]["messageId"]  # held from now on


def test_a_rooms_first_turn_observes_the_whole_window(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    _room_turn(f.message(messageSeq=3, text="plan?"), f.message(messageSeq=4, author=ME, text="on it"))
    assert [r["content"] for r in db.transcripts[ROOM_KEY]] == ["[Prez|user_1]\nplan?", "[CEO|ceo]\non it"]
    assert _said(built)["conversation_history"] is None


def test_a_window_with_nothing_held_says_older_messages_are_missing(hermes, home):
    """More arrived than the backend's 50-message window: the window does not reach anything
    the session already holds."""
    _acquired, _built, dbs = hermes
    db = _room_db(dbs, transcripts={ROOM_KEY: [{"role": "user", "content": "Prez: hi", "message_id": "m1"}]})
    _room_turn(f.message(messageId="m60", messageSeq=60, text="late"))
    assert [r["content"] for r in db.transcripts[ROOM_KEY][1:]] == [turn.OBSERVED_GAP, "[Prez|user_1]\nlate"]


def test_nothing_unheld_leaves_the_message_as_it_was(hermes, home):
    _acquired, built, dbs = hermes
    _room_db(dbs, transcripts={ROOM_KEY: [{"role": "user", "content": "Prez: seen", "message_id": "m9"}]})
    _room_turn(f.message(messageId="m9", messageSeq=9, text="seen"))
    call = _said(built)
    assert f.split_turn_input(call["user_message"])[1] == "Prez: now" and "persist_user_message" not in call


def test_a_follow_up_records_no_second_platform_id_for_its_trigger(hermes, home):
    _acquired, built, dbs = hermes
    trigger = f.message(messageId="t1", messageSeq=7, text="review it")
    _room_db(dbs, transcripts={ROOM_KEY: [{"role": "user", "content": "Prez: review it", "message_id": "t1"}]})
    body = f.ingress(message=trigger, eventId="fu-1",
                     followUp={"id": "fu-1", "reason": "delegation_completed", "text": "[IMPORTANT: done]"},
                     recentHistory=[trigger, f.message(messageSeq=9, author=ME, text="I'll report back",
                                                       replyToMessageId="t1")])
    turn.parse_ingress(body)
    assert turn._run_hermes("ceo", body, None)[0] == "completed"
    call = _said(built)
    assert "persist_user_platform_id" not in call
    assert f.split_turn_input(call["user_message"])[1] == "[IMPORTANT: done]"  # trigger and promise held


def test_an_earlier_image_is_a_saved_at_note_every_later_turn_reopens(hermes, home, monkeypatch):
    """Hermes's own media note (CachedMedia.context_note). The room's turn folder has the same
    path on every turn and is re-filled with the window's attachments, so the note stays openable
    for as long as the message is in the window -- and nothing outlives the turn."""
    _acquired, _built, dbs = hermes
    db = _room_db(dbs)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: b"png")
    att = {"id": "img-1", "fileName": "past.png", "contentType": "image/png", "size": 3}
    _room_turn(f.message(messageId="m5", messageSeq=5, text="look", attachments=[att]))
    path = sync.turn_media_root() / f"ceo.{f.ROOM}" / "img-1.png"
    assert db.transcripts[ROOM_KEY][0]["content"] == f"[Prez|user_1]\nlook\n\n[image 'past.png' saved at: {path}]"
    assert not path.exists()


def test_a_room_turn_without_a_session_db_fails(hermes, home, monkeypatch):
    monkeypatch.setattr(turn, "_session_dbs", None)
    monkeypatch.delitem(sys.modules, "hermes_state_registry", raising=False)
    with pytest.raises(RuntimeError, match="state.db unavailable"):
        turn._construct_agent("ceo", f.ingress(), None, turn.lightbulb_session_key("ceo", f.ROOM))


def test_a_raising_run_still_ends_a_non_room_session(hermes, home):
    _acquired, _built, dbs = hermes
    SCRIPT.append(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        _run_paperclip()
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    assert db.ended == [(PAPERCLIP["sessionId"], "agent_close")]


def test_an_unreadable_transcript_fails_the_turn(hermes, home):
    """As Hermes's gateway: a failed canonical read is not an empty conversation."""
    _acquired, _built, dbs = hermes
    _room_db(dbs, transcripts={ROOM_KEY: sqlite3.DatabaseError("malformed")})
    with pytest.raises(sqlite3.DatabaseError):
        _room_turn()


# ---- the review counters carry across turns ------------------------------------------------------
def test_the_skill_counter_survives_the_per_turn_agent(hermes, home):
    """Restored at construction from the session row, saved after the run: the skill review's
    tool-iteration count is never hydrated from history by Hermes."""
    _acquired, built, dbs = hermes
    key = PAPERCLIP["sessionId"]
    db = _room_db(dbs, meta={key: {turn.SKILL_COUNTER: 7}})

    def work(agent):
        assert agent._iters_since_skill == 7
        agent._iters_since_skill = 11
        return {"api_calls": 1, "final_response": "ok"}

    SCRIPT.append(work)
    _run_paperclip()
    assert db.meta[key] == {turn.SKILL_COUNTER: 11, turn.PRESSURE: 0.0}  # the fake has no compressor


# ---- a state.db failure fails the turn, as in Hermes's gateway -----------------------------------
def test_a_state_db_failure_is_reported_never_rerun(hermes, home):
    """Hermes stops the round before its tools run; a rerun could repeat an earlier round's."""
    _acquired, built, _dbs = hermes
    SCRIPT.append({"api_calls": 1, "final_response": "", "failed": True,
                   "turn_exit_reason": "session_persistence_failed", "messages": []})
    assert _run_paperclip_outcome() == "failed"
    assert len(built[0].calls) == 1


def _run_paperclip_outcome():
    return turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[0]


def test_an_interrupted_turn_is_an_error_never_a_reply(hermes, home):
    """Hermes's own stop (liveness watchdog, lease) can carry partial text or its own
    "Your message was not processed"; neither may reach the room."""
    SCRIPT.append({"api_calls": 1, "final_response": "half an answ", "interrupted": True})
    assert turn._run_native_paperclip(PAPERCLIP, {"input": "go", "instructions": "do"})[:3] == (
        "failed", turn.INTERRUPTED, "")
    SCRIPT.append({"api_calls": 0, "final_response": "Your message was not processed.", "interrupted": True})
    assert turn._run_hermes("ceo", f.ingress(), None)[:3] == ("error", turn.INTERRUPTED, "")


# ---- a call has its own transcript; the room keeps one record per spoken exchange -----------------
def _persisting(reply, **result):
    """A run_conversation that records its turn the way Hermes does (persist_user_message wins)."""
    def run(agent):
        kw = agent.calls[-1][0]
        rows = agent._session_db.transcripts.setdefault(agent.session_id, [])
        pid = kw.get("persist_user_platform_id")
        rows.append({"role": "user", "content": kw.get("persist_user_message") or kw["user_message"],
                     **({"message_id": pid} if pid else {})})
        rows.append({"role": "assistant", "content": reply})
        return {"api_calls": 1, "final_response": reply, **result}
    return run


def _records(db, sid=None):
    return [r for r in db.transcripts.get(sid or ROOM_KEY, []) if str(r.get("message_id", "")).startswith(turn.CALL_RECORD_ID)]


def test_a_call_turn_runs_on_the_calls_transcript_not_the_rooms(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.extend([_persisting("Shipped."), _persisting("Payouts landed.")])
    _room_turn()
    _run_call("Aden (voice): check payouts")
    assert built[-1].kw["session_id"] == built[-1].kw["gateway_session_key"] == CALL_KEY
    assert _said(built)["conversation_history"] is None  # nothing of the room's transcript is replayed
    assert db.transcripts[CALL_KEY] == [{"role": "user", "content": _spoken("Aden (voice): check payouts")},
                                        {"role": "assistant", "content": "Payouts landed."}]
    # The room session got the record and no transcript rows.
    assert [r["role"] for r in db.transcripts[ROOM_KEY]] == ["user", "assistant", "user"]


def test_two_turns_of_one_call_share_a_transcript_and_the_next_call_does_not(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.extend([_persisting("Six agents."), _persisting("CEO, Engineering and four more."), _persisting("Fifty dollars.")])
    _run_call("Aden (voice): how many agents?", run_id="r1")
    _run_call("Aden (voice): and what are they called?", run_id="r2")
    assert _said(built)["conversation_history"] == [{"role": "user", "content": _spoken("Aden (voice): how many agents?")},
                                                     {"role": "assistant", "content": "Six agents."}]
    _run_call("Aden (voice): what does the plan cost?", run_id="r3", call_id=NEXT_CALL_ID)
    said = _said(built)
    assert built[-1].kw["session_id"] == NEXT_CALL_KEY and said["conversation_history"] is None
    # A new call starts with the room's records of the earlier one, as observed context only.
    assert said["user_message"].startswith(turn.OBSERVED_HEADER) and "I answered: Six agents." in said["user_message"]
    assert [r["message_id"] for r in _records(db, NEXT_CALL_KEY)] == ["call:r1", "call:r2"]
    assert [r["message_id"] for r in _records(db)] == ["call:r1", "call:r2", "call:r3"]


def test_the_room_gets_one_compact_record_per_answered_turn_under_its_turn_lock(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    held, append = [], db.append_message

    def locked_append(session_id, **kw):
        if session_id == ROOM_KEY:
            held.append(turn._turn_lock("ceo", ROOM_KEY).locked())
        return append(session_id=session_id, **kw)

    db.append_message = locked_append
    tool_round = [{"role": "user", "content": "q"},
                  {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}}]},
                  {"role": "tool", "content": "{}"}, {"role": "assistant", "content": "Payouts  landed\ntoday."}]
    SCRIPT.extend([_persisting("Payouts  landed\ntoday.", messages=tool_round), _persisting("again")])
    _run_call("Aden (voice): check payouts")
    assert _records(db) == [{"role": "user", "observed": True, "message_id": "call:run-call", "content": (
        "[CEO|ceo]\n(On a Live voice call; spoken, not posted to this room.) Aden asked: check payouts\n"
        "I answered: Payouts landed today.\nTools I used: terminal")}]
    assert held == [True] and len(built) == 1 and len(built[0].calls) == 1  # no model call of its own
    turn.RUNS.clear()
    _run_call("Aden (voice): check payouts")  # the same run, run again after a lost registry
    assert len(_records(db)) == 1


def test_the_next_room_turn_reads_the_call_record_as_observed_context(hermes, home):
    _acquired, built, dbs = hermes
    _room_db(dbs)
    SCRIPT.extend([_persisting("Payouts landed."), _persisting("ok")])
    _run_call("Aden (voice): check payouts")
    _room_turn()
    said = _said(built)
    assert said["conversation_history"] is None  # the call's turns are not the room's history
    assert said["user_message"].startswith(turn.OBSERVED_HEADER)
    assert "Aden asked: check payouts\nI answered: Payouts landed." in said["user_message"]


def test_a_call_answered_while_the_room_is_busy_records_it_at_the_rooms_next_turn(hermes, home):
    """A call holds its own lock, never the room's: the room may be mid-turn, so the record waits
    and the room's next turn writes it before reading its history. A new call carries it meanwhile."""
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.extend([_persisting("Payouts landed."), _persisting("Still there."), _persisting("ok")])
    room = turn._turn_lock("ceo", ROOM_KEY)
    with room:  # a long chat turn holds the room
        _run_call("Aden (voice): check payouts")  # runs at once: it does not wait for the room
        assert _records(db) == []
        _run_call("Aden (voice): and now?", run_id="r2", call_id=NEXT_CALL_ID)
        assert [r["message_id"] for r in _records(db, NEXT_CALL_KEY)] == ["call:run-call"]
    _room_turn()
    assert [r["message_id"] for r in _records(db)] == ["call:run-call", "call:r2"]
    assert "I answered: Payouts landed." in _said(built)["user_message"]
    assert not turn._PENDING_CALL_RECORDS


@pytest.mark.parametrize("result, outcome", [
    ({"api_calls": 1, "failed": True, "final_response": ""}, "error"),
    ({"api_calls": 1, "final_response": "", "interrupted": True}, "error"),
    ({"api_calls": 1, "final_response": ""}, "error"),
    ("stop", "aborted"),
], ids=["failed", "interrupted", "empty", "stopped"])
def test_a_turn_that_gave_no_answer_leaves_no_room_record(hermes, home, result, outcome):
    _acquired, _built, dbs = hermes
    db = _room_db(dbs)
    stop = lambda _agent: turn.abort_run("run-call") and {"api_calls": 1, "final_response": "Half an answer."}  # noqa: E731
    SCRIPT.append(stop if result == "stop" else result)
    _run_call(outcome=outcome)
    assert _records(db) == []
    assert not turn._turn_lock("ceo", ROOM_KEY).locked() and not turn._turn_lock("ceo", CALL_KEY).locked()


def test_a_late_steer_in_a_call_is_answered_in_the_same_run_and_record(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.extend([{"api_calls": 1, "final_response": "First.", "pending_steer": "Aden (voice): and Y",
                    "messages": [{"role": "user", "content": "x"}]},
                   {"api_calls": 1, "final_response": "Second."}])
    _run_call()
    assert [c[0]["user_message"] for c in built[-1].calls] == [_spoken("Aden (voice): hi"), "Aden (voice): and Y"]
    assert len(built) == 1 and built[0].kw["session_id"] == CALL_KEY  # one agent, one run, the call's transcript
    (record,) = _records(db)
    assert record["content"].endswith("I answered: First. Second.")


def test_the_rooms_recent_messages_still_reach_a_call_turn(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.extend([_persisting("Noted."), _persisting("ok")])
    earlier = f.message(messageId="m1", messageSeq=1, author=ANA, text="the deploy is red")
    _run_call("Aden (voice): what's up?", history=[earlier])
    assert _said(built)["user_message"] == _observed("[Ana|user_2]\nthe deploy is red") + _spoken("Aden (voice): what's up?")
    assert [r.get("message_id") for r in db.transcripts[CALL_KEY] if r.get("observed")] == ["m1"]
    _room_turn(earlier)
    # The room observes it for itself, once: the call's rows are not the room's.
    assert sum(1 for r in db.transcripts[ROOM_KEY] if r.get("message_id") == "m1") == 1
    assert _said(built)["user_message"].count("the deploy is red") == 1


# ---- retention -----------------------------------------------------------------------------------
def test_every_session_but_a_rooms_is_ended_after_its_turn(hermes, home):
    """Hermes prunes only ended sessions; a per-turn agent never closes, so none ever ended."""
    _acquired, built, dbs = hermes
    _run_paperclip()
    _run_call()
    _run_room()
    db = dbs[sync._profile_dir("ceo") / "state.db"]
    # A call's own session is ended too, so Hermes's retention ages it out; the room's never is.
    assert db.ended == [(PAPERCLIP["sessionId"], "agent_close"), (CALL_KEY, "agent_close")]


def test_retention_runs_once_per_process_when_state_db_is_first_opened(hermes, home):
    _acquired, _built, dbs = hermes
    _run_paperclip()
    _run_paperclip()
    assert dbs[sync._profile_dir("ceo") / "state.db"].maintained == [90]


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


def test_the_curator_never_runs_while_a_delegation_is_live(curator, monkeypatch):
    from bridge import followups
    runs, _saved = curator
    monkeypatch.setattr(followups, "busy", lambda: True)
    turn._busy_add(1)
    turn._end_turn()
    assert runs == []


def test_a_curator_that_is_not_due_runs_nothing(curator, monkeypatch):
    runs, _saved = curator
    monkeypatch.setattr(sys.modules["agent.curator"], "should_run_now", lambda: False)
    turn._busy_add(1)
    turn._end_turn()
    assert runs == []


# ---- a typed turn behind a long job in its room runs beside it ----------------------------------
RUN_ID = "22222222-2222-4222-8222-222222222222"  # factories.ingress's run


def test_a_turn_behind_a_long_job_runs_in_its_own_session_with_the_rooms_window(hermes, home):
    _acquired, built, dbs = hermes
    db = _room_db(dbs)
    SCRIPT.append(_persisting("MRR is $4k."))
    side = turn.side_session_key("ceo", RUN_ID)
    with turn._turn_lock("ceo", ROOM_KEY):  # the room's long job still holds it
        # What run_turn hands a turn it moved beside its busy room (_side_session).
        body = _room_turn(f.message(messageSeq=9, text="earlier"), sessionKey=side, **{turn.ROOM_KEY_FIELD: ROOM_KEY})
    assert built[-1].kw["session_id"] == side
    assert "earlier" in _said(built)["user_message"]  # the room's recent window, as observed rows
    assert ROOM_KEY not in db.transcripts  # the room's own transcript is untouched
    assert not turn._turn_lock("ceo", side).locked()


def test_a_turn_behind_a_short_job_waits_for_the_room_and_a_long_one_does_not(monkeypatch):
    room = turn._turn_lock("ceo", ROOM_KEY)
    room.acquire()
    turn._lock_since[("ceo", ROOM_KEY)] = turn.time.monotonic()  # the job ahead just started
    monkeypatch.setattr(turn.time, "sleep", lambda _s: room.release())  # it finishes while we wait
    assert turn._side_session("ceo", ROOM_KEY, RUN_ID) is False
    with room:
        turn._lock_since[("ceo", ROOM_KEY)] = turn.time.monotonic() - 31
        assert turn._side_session("ceo", ROOM_KEY, RUN_ID) is True
    assert turn._side_session("ceo", turn.call_session_key("ceo", CALL_ID), RUN_ID) is False  # rooms only


def test_the_long_job_clock_starts_when_the_job_has_a_turn_slot(monkeypatch):
    """A turn holding its room's lock while it waits for a microVM-wide slot has started nothing."""
    monkeypatch.setattr(turn, "_turn_slots", turn.threading.BoundedSemaphore(1))
    turn._turn_slots.acquire()  # every slot busy
    started = turn.threading.Event()

    def queued():
        with turn._waiting_turn_lock("ceo", ROOM_KEY, None):
            started.set()

    thread = turn.threading.Thread(target=queued)
    thread.start()
    while not turn._turn_lock("ceo", ROOM_KEY).locked():
        turn.time.sleep(0.01)
    assert ("ceo", ROOM_KEY) not in turn._lock_since  # holds the room, waits for a slot
    turn._turn_slots.release()
    thread.join(5)
    assert started.is_set() and ("ceo", ROOM_KEY) not in turn._lock_since  # released with the lock
