"""Delegation-completion drain + follow-up turns + persistence gaps (contract §A, §D, §E, §F)."""
import queue
import sys
import threading
import types
import uuid

import pytest

import factories as f
from bridge import followups, server, sync, turn

SESSION = turn.lightbulb_session_key("ceo", f.ROOM)
ROOT = "33333333-3333-4333-8333-333333333333"
TRIGGER = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    turn.LAST_TURN.clear()
    turn.DELEGATION_TURN.clear()
    turn.RESTORED.clear()
    followups._attempts.clear()
    followups._TASKS.clear()
    monkeypatch.setattr(followups, "_retrying", 0)
    yield
    turn.LAST_TURN.clear()
    turn.DELEGATION_TURN.clear()
    turn.RESTORED.clear()
    followups._attempts.clear()


def _follow_up_body(text="subagent says: done", **over):
    fid = str(uuid.uuid4())
    msg = f.message(messageId=TRIGGER, threadRootMessageId=ROOT)
    body = f.ingress(message=msg, eventId=fid, followUp={"id": fid, "reason": "delegation_completed", "text": text})
    body.update(over)
    return body


# ---- D: parse_ingress ------------------------------------------------------------------------
def test_a_follow_up_skips_the_event_id_equals_message_id_check():
    assert turn.parse_ingress(_follow_up_body())["followUp"]["reason"] == "delegation_completed"


def test_without_a_follow_up_the_event_id_check_still_applies():
    body = f.ingress()
    body["eventId"] = str(uuid.uuid4())
    with pytest.raises(turn.IngressError, match="eventId must equal"):
        turn.parse_ingress(body)


@pytest.mark.parametrize("bad", [
    "not a dict",
    {"id": "other", "reason": "delegation_completed", "text": "x"},
    {"reason": "delegation_completed", "text": "x"},
    {"id": None, "reason": "whim", "text": "x"},
    {"id": None, "reason": "delegation_completed", "text": 5},
    {"id": None, "reason": "delegation_completed", "text": "é" * 16385},  # 32770 bytes
])
def test_a_malformed_follow_up_is_refused(bad):
    body = _follow_up_body()
    if isinstance(bad, dict) and "id" in bad and bad["id"] is None:
        bad = {**bad, "id": body["eventId"]}
    body["followUp"] = bad
    with pytest.raises(turn.IngressError, match="followUp"):
        turn.parse_ingress(body)


# ---- D: the follow-up turn -------------------------------------------------------------------
class _Agent:
    session_input_tokens = session_output_tokens = 0
    session_cache_read_tokens = session_cache_write_tokens = 0

    def __init__(self, seen):
        self.seen = seen

    def run_conversation(self, **kw):
        self.seen.update(kw)
        return {"api_calls": 1, "final_response": "PR opened"}


def test_a_follow_up_turn_feeds_the_follow_up_text_and_replies_in_the_original_thread(home, posted, monkeypatch):
    seen = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a: _Agent(seen))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    body = _follow_up_body(eventKind="room_event")
    turn.run_turn("ceo", body)
    _, text = f.split_turn_input(seen["user_message"])
    assert text == "subagent says: done"
    reply = next(b for p, b in posted if p == "agent-messages/org_test")
    assert reply["threadRootMessageId"] == ROOT
    assert reply["replyToMessageId"] == TRIGGER
    assert reply["eventId"] == f"{body['eventId']}:ceo:reply"


def test_a_room_turn_records_last_turn_and_starts_the_drain_once(home, posted, monkeypatch):
    loops = []
    monkeypatch.setattr(followups, "_started", False)
    monkeypatch.setattr(followups, "_loop", lambda: loops.append(threading.current_thread().name))
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "", turn.ZERO_USAGE))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    body = f.ingress(message=f.message(messageId=TRIGGER))
    turn.run_turn("ceo", body)
    turn.run_turn("ceo", f.ingress(message=f.message(), run={**body["run"], "runId": str(uuid.uuid4())}))
    for t in threading.enumerate():
        if t.name == "delegation-drain":
            t.join(1)
    assert loops == ["delegation-drain"]
    assert turn.LAST_TURN[SESSION]["roomId"] == f.ROOM
    assert turn.LAST_TURN[SESSION]["agentId"] == "ceo"
    assert turn.LAST_TURN[SESSION]["eventKind"] == "user_request"
    assert turn.LAST_TURN[SESSION]["triggerMessageId"] != TRIGGER  # the latest room turn wins


def test_importing_the_bridge_starts_nothing():
    """Warm-pool microVMs import this and must do nothing session-bound."""
    import os
    import pathlib
    import subprocess
    code = ("import threading, bridge.server as s; "
            "assert not s.followups._started; "
            "assert [t.name for t in threading.enumerate()] == ['MainThread']")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=pathlib.Path(__file__).resolve().parents[1],
                   env={**os.environ, "LIGHTBULB_SECRET_ID": ""})


def test_a_mail_turn_does_not_start_the_drain(home, posted, monkeypatch):
    monkeypatch.setattr(followups, "_started", False)
    monkeypatch.setattr(followups, "ensure_started", lambda: pytest.fail("no drain for a run-less turn"))
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "", turn.ZERO_USAGE))
    turn.run_turn("ceo", {"version": 1, "eventId": "e", "eventKind": "agent_mail", "targetAgentId": "ceo",
                          "message": {"text": "hi"}})


# ---- E: the drain ----------------------------------------------------------------------------
class _Ledger:
    """Fake tools.async_delegation: records every ledger transition."""

    def __init__(self, owned_elsewhere=()):
        self.calls, self.owned_elsewhere, self.live = [], set(owned_elsewhere), 0

    def claim_event_delivery(self, evt, consumer):
        assert consumer == "lightbulb-bridge"
        if evt["delegation_id"] in self.owned_elsewhere:
            return None
        self.calls.append(("claim", evt["delegation_id"]))
        return f"c-{evt['delegation_id']}"

    def complete_event_delivery(self, evt, claim):
        self.calls.append(("complete", evt["delegation_id"], claim))

    def release_event_delivery(self, evt, claim):
        self.calls.append(("release", evt["delegation_id"], claim))

    def drop_completion_delivery(self, delegation_id, claim):
        self.calls.append(("drop", delegation_id, claim))

    def active_count(self):
        return self.live


def _evt(did, session=SESSION):
    return {"type": "async_delegation", "delegation_id": did, "session_key": session, "summary": did}


def _hermes(monkeypatch, events, ledger):
    q = queue.Queue()
    for e in events:
        q.put(e)
    monkeypatch.setattr(followups, "_hermes", lambda: (types.SimpleNamespace(completion_queue=q), ledger,
                                                        lambda evt: f"[IMPORTANT: {evt['summary']}]"))
    return q


def _remember_turn():
    turn.LAST_TURN[SESSION] = {"roomId": f.ROOM, "agentId": "ceo", "triggerMessageId": TRIGGER,
                               "eventKind": "user_request"}


def _drained(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_session_group_is_claimed_formatted_and_posted_once(monkeypatch):
    _remember_turn()
    ledger = _Ledger()
    process = {"type": "completion", "session_id": "proc_1"}
    q = _hermes(monkeypatch, [_evt("d2"), process, _evt("d1")], ledger)
    calls = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: (calls.append((path, body)), 202)[1])
    followups.drain_once()
    [(path, body)] = calls
    assert path == "followups/org_test"
    assert body["followUpId"] == str(uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb-followup:d1,d2"))
    assert body | {"text": None, "followUpId": None} == {
        "followUpId": None, "roomId": f.ROOM, "agentId": "ceo", "triggerMessageId": TRIGGER,
        "eventKind": "user_request", "reason": "delegation_completed", "text": None}
    assert "2 background subagent delegations" in body["text"]
    assert "[IMPORTANT: d1]" in body["text"] and "[IMPORTANT: d2]" in body["text"]
    assert ("complete", "d1", "c-d1") in ledger.calls and ("complete", "d2", "c-d2") in ledger.calls
    assert _drained(q) == [process]  # out of scope: re-queued untouched
    assert not followups.busy()


def test_the_follow_up_text_is_capped_at_32768_utf8_bytes(monkeypatch):
    _remember_turn()
    q = queue.Queue()
    q.put(_evt("d1"))
    monkeypatch.setattr(followups, "_hermes", lambda: (types.SimpleNamespace(completion_queue=q), _Ledger(),
                                                        lambda evt: "é" * 40000))
    calls = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: (calls.append(body), 202)[1])
    followups.drain_once()
    assert len(calls[0]["text"].encode()) <= 32768


def test_a_4xx_is_final_and_drops_the_delivery(monkeypatch):
    _remember_turn()
    ledger = _Ledger()
    q = _hermes(monkeypatch, [_evt("d1")], ledger)
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: 404)
    followups.drain_once()
    assert ("drop", "d1", "c-d1") in ledger.calls
    assert _drained(q) == []
    assert not followups.busy()


@pytest.mark.parametrize("status", [503, 0])
def test_a_5xx_or_network_error_releases_and_requeues(monkeypatch, status):
    _remember_turn()
    ledger = _Ledger()
    q = _hermes(monkeypatch, [_evt("d1")], ledger)
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: status)
    followups.drain_once()
    assert ("release", "d1", "c-d1") in ledger.calls
    assert [e["delegation_id"] for e in _drained(q)] == ["d1"]
    assert followups.busy()  # a pending retry keeps the microVM alive


def test_a_session_with_no_room_turn_is_dropped_not_posted(monkeypatch):
    ledger = _Ledger()
    q = _hermes(monkeypatch, [_evt("d1", session="lightbulb-email:thread-1")], ledger)
    monkeypatch.setattr(turn, "_post_backend", lambda *a: pytest.fail("nowhere to post"))
    followups.drain_once()
    assert ledger.calls == [("claim", "d1"), ("drop", "d1", "c-d1")]
    assert _drained(q) == []


def test_an_event_claimed_by_another_consumer_is_left_alone(monkeypatch):
    _remember_turn()
    ledger = _Ledger(owned_elsewhere={"d1"})
    q = _hermes(monkeypatch, [_evt("d1")], ledger)
    monkeypatch.setattr(turn, "_post_backend", lambda *a: pytest.fail("not ours"))
    followups.drain_once()
    assert _drained(q) == []


def test_the_ledger_is_touched_under_the_agents_hermes_home(monkeypatch):
    """The durable ledger is <HERMES_HOME>/state.db -- the PROFILE's, where the turn dispatched it."""
    _remember_turn()
    scopes = []
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda agent_id: (scopes.append(agent_id), __import__("contextlib").nullcontext())[1])
    _hermes(monkeypatch, [_evt("d1")], _Ledger())
    monkeypatch.setattr(turn, "_post_backend", lambda *a: 202)
    followups.drain_once()
    assert scopes == ["ceo"]


def test_the_drain_is_inert_outside_the_hermes_image():
    assert followups._hermes() is None or "tools" in sys.modules
    followups.drain_once()  # must not raise


# ---- /ping -----------------------------------------------------------------------------------
def test_ping_is_busy_while_a_turn_runs_or_a_delegation_is_live(monkeypatch):
    assert server._ping_status() == "Healthy"
    ledger = _Ledger()
    fake = types.ModuleType("tools.async_delegation")
    fake.active_count = ledger.active_count
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake)
    ledger.live = 1
    assert server._ping_status() == "HealthyBusy"
    ledger.live = 0
    assert server._ping_status() == "Healthy"
    turn._busy_add(1)
    assert server._ping_status() == "HealthyBusy"


def test_ping_never_imports_hermes_itself(monkeypatch):
    monkeypatch.delitem(sys.modules, "tools.async_delegation", raising=False)
    assert server._ping_status() == "Healthy"
    assert "tools.async_delegation" not in sys.modules


def test_ping_carries_no_timestamp_so_the_idle_timeout_can_fire(monkeypatch):
    sent = []
    handler = object.__new__(server.Handler)
    handler.path = "/ping"
    monkeypatch.setattr(handler, "_send", lambda code, obj: sent.append((code, obj)), raising=False)
    handler.do_GET()
    assert sent == [(200, {"status": "Healthy"})]


# ---- F: post-turn background writes ----------------------------------------------------------
class _MemoryManager:
    def __init__(self, order):
        self.order = order

    def flush_pending(self, timeout=None):
        self.order.append("memory")
        return True


def test_save_waits_for_the_review_and_memory_sync_to_finish(home, posted, monkeypatch):
    order = []
    review = types.SimpleNamespace(request_done=threading.Event())

    class _Agent2(_Agent):
        _memory_manager = _MemoryManager(order)
        _background_review_run = review

        def run_conversation(self, **kw):
            threading.Thread(target=lambda: (threading.Event().wait(0.3), order.append("review"),
                                             review.request_done.set()), name="bg-review").start()
            return {"api_calls": 1, "final_response": "done"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: _Agent2({}))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    monkeypatch.setattr(turn, "save_profile", lambda agent_id: order.append("save"))
    turn.run_turn("ceo", f.ingress())
    assert order == ["review", "memory", "save"]
    assert any(p == "agent-messages/org_test" for p, _b in posted)


def test_a_review_thread_that_exits_without_signalling_does_not_hold_the_save():
    """_run_review_in_thread's provider-skip path returns before finish_background_review_run."""
    agent = types.SimpleNamespace(_background_review_run=types.SimpleNamespace(request_done=threading.Event()))
    t0 = __import__("time").monotonic()
    turn._await_post_turn_writes(agent, ceiling=30)
    assert __import__("time").monotonic() - t0 < 5


def test_a_hung_review_is_abandoned_at_the_ceiling():
    stop = threading.Event()
    hung = threading.Thread(target=stop.wait, name="bg-review", daemon=True)
    hung.start()
    try:
        agent = types.SimpleNamespace(_background_review_run=types.SimpleNamespace(request_done=threading.Event()))
        turn._await_post_turn_writes(agent, ceiling=0.2)
    finally:
        stop.set()


# ---- F: SIGTERM ------------------------------------------------------------------------------
def test_sigterm_saves_every_restored_profile_and_root_auth_then_exits(monkeypatch):
    """Every restored profile, then root auth, then a bounded exit."""
    saved, exits = [], []
    turn.RESTORED.update({"ceo", "eng"})
    monkeypatch.setattr(sync, "save_profile", saved.append)
    monkeypatch.setattr(sync, "save_root_auth", lambda: saved.append("root"))
    monkeypatch.setattr(server.os, "_exit", exits.append)
    server._save_on_sigterm(15, None)
    assert saved == ["ceo", "eng", "root"]
    assert exits == [143]


def test_restore_records_the_profile_for_sigterm(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "", turn.ZERO_USAGE))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    turn.run_turn("ceo", f.ingress())
    assert "ceo" in turn.RESTORED


# ---- review fixes ------------------------------------------------------------------------------
def test_a_follow_up_may_carry_history_at_and_after_its_trigger():
    """The follow-up must see the request and its own promise, both at/after message.messageSeq."""
    body = _follow_up_body()
    promise = f.message(messageSeq=9, author={"kind": "agent", "id": "ceo", "displayName": "CEO"},
                        text="review running, I'll report back")
    body["recentHistory"] = [f.message(messageId=TRIGGER, messageSeq=7), promise]
    assert turn.parse_ingress(body)["recentHistory"][-1]["text"] == "review running, I'll report back"
    bad = _follow_up_body()
    bad["recentHistory"] = [f.message(messageSeq=9), f.message(messageSeq=8)]
    with pytest.raises(turn.IngressError, match="strictly increase"):
        turn.parse_ingress(bad)


def test_without_a_follow_up_history_must_stay_below_the_message():
    body = f.ingress()
    body["recentHistory"] = [f.message(messageSeq=9)]
    with pytest.raises(turn.IngressError, match="below message.messageSeq"):
        turn.parse_ingress(body)


def _fake_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_the_turn_binds_the_room_session_key_for_async_delegations(home, posted, monkeypatch):
    """delegate_tool_dispatch._resolve_async_session_key stamps get_current_session_key(); unbound, it
    falls back to agent.session_id, which compression renames, orphaning the completion."""
    import contextvars
    var = contextvars.ContextVar("k", default="")
    _fake_module(monkeypatch, "tools")
    _fake_module(monkeypatch, "tools.approval_context", set_current_session_key=var.set,
                 reset_current_session_key=var.reset, get_current_session_key=lambda default="": var.get() or default)
    seen = {}

    class _Probe(_Agent):
        def run_conversation(self, **kw):
            seen["key"] = var.get()
            return super().run_conversation(**kw)

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: _Probe({}))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    turn.run_turn("ceo", f.ingress())
    assert seen["key"] == SESSION
    assert var.get() == ""  # reset after the turn


def test_the_delegation_maps_to_the_turn_that_started_it_not_the_latest(home, posted, monkeypatch):
    records = []
    _fake_module(monkeypatch, "tools.async_delegation", list_async_delegations=lambda: list(records),
                 active_count=lambda: 0)

    def run_hermes(*a, **k):
        records.append({"delegation_id": "d1", "session_key": SESSION, "status": "running"})
        return ("completed", None, "", turn.ZERO_USAGE)

    monkeypatch.setattr(turn, "_run_hermes", run_hermes)
    monkeypatch.setattr(turn, "publish_frame", lambda *a: 200)
    turn.run_turn("ceo", f.ingress(message=f.message(messageId=TRIGGER)))
    assert turn.DELEGATION_TURN["d1"]["triggerMessageId"] == TRIGGER
    # A later turn in the same session keeps d1 on its own turn.
    later = f.ingress(message=f.message(), run={"runId": str(uuid.uuid4()), "frameEndpoint": "", "frameChannel": ""})
    turn.run_turn("ceo", later)
    assert turn.DELEGATION_TURN["d1"]["triggerMessageId"] == TRIGGER
    assert turn.LAST_TURN[SESSION]["triggerMessageId"] == later["message"]["messageId"]


def test_the_drain_resolves_by_delegation_before_last_turn(monkeypatch):
    _remember_turn()
    turn.DELEGATION_TURN["d1"] = {"roomId": f.ROOM, "agentId": "ceo", "triggerMessageId": ROOT,
                                  "eventKind": "room_event"}
    ledger = _Ledger()
    _hermes(monkeypatch, [_evt("d1"), _evt("d2")], ledger)
    calls = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: (calls.append(body), 202)[1])
    followups.drain_once()
    by_trigger = {b["triggerMessageId"]: b for b in calls}
    assert by_trigger[ROOT]["eventKind"] == "room_event" and "[IMPORTANT: d1]" in by_trigger[ROOT]["text"]
    assert "[IMPORTANT: d2]" in by_trigger[TRIGGER]["text"]
    assert "d1" not in turn.DELEGATION_TURN  # delivered: forgotten


def _notice(did, index):
    return {**_evt(did), "task_failure_notice": True, "results": [{"task_index": index, "status": "failed"}]}


def test_each_interim_notice_of_a_batch_gets_its_own_follow_up_id(monkeypatch):
    _remember_turn()
    calls = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: (calls.append(body["followUpId"]), 202)[1])
    _hermes(monkeypatch, [_notice("d1", 0)], _Ledger())
    followups.drain_once()
    _hermes(monkeypatch, [_notice("d1", 1)], _Ledger())
    followups.drain_once()
    assert len(set(calls)) == 2


def test_a_retry_budget_bounds_sustained_5xx(monkeypatch):
    _remember_turn()
    q = _hermes(monkeypatch, [_notice("d1", 0)], _Ledger())
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: 503)
    for _ in range(7):
        followups.drain_once()
        assert followups.busy()
    followups.drain_once()
    assert _drained(q) == []
    assert not followups.busy()


# ---- audit #9: a Worker session has no room turn: its completions go to the task's /inputs -------
TASK = "55555555-5555-4555-8555-555555555555"
TASK_SESSION = f"agent:ceo:lightbulb:task:{TASK}"


@pytest.mark.parametrize("status,final", [(202, "complete"), (403, "drop"), (503, "release")])
def test_a_worker_sessions_completion_becomes_a_task_input(monkeypatch, status, final):
    ledger = _Ledger()
    q = _hermes(monkeypatch, [_evt("d1", TASK_SESSION), _evt("d2", TASK_SESSION)], ledger)
    followups.remember_task(TASK_SESSION, TASK, "secret-1")
    sent = []
    monkeypatch.setattr(followups, "_post_input", lambda body: sent.append(body) or status)
    monkeypatch.setattr(turn, "_post_backend", lambda *a: pytest.fail("no room follow-up for a task session"))

    followups.drain_once()

    body = sent[0]
    assert {k: body[k] for k in ("taskId", "sessionKey", "kind", "jobId", "runSecret")} == {
        "taskId": TASK, "sessionKey": TASK_SESSION, "kind": "delegation_completed", "jobId": "d1,d2",
        "runSecret": "secret-1"}
    assert "[IMPORTANT: d1]" in body["text"] and "[IMPORTANT: d2]" in body["text"]
    assert body["deliveryId"] == str(uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb-task-input:d1,d2"))  # stable on retry
    assert [c[0] for c in ledger.calls if c[0] != "claim"] == [final, final]
    assert q.qsize() == (2 if final == "release" else 0)


def test_an_unknown_session_with_no_room_turn_is_still_dropped(monkeypatch):
    ledger = _Ledger()
    _hermes(monkeypatch, [_evt("d1", TASK_SESSION)], ledger)
    monkeypatch.setattr(followups, "_post_input", lambda body: pytest.fail("no task known"))
    followups.drain_once()
    assert ("drop", "d1", "c-d1") in ledger.calls


def test_a_worker_run_registers_its_task_and_starts_the_drain(monkeypatch):
    seen = []
    monkeypatch.setattr(followups, "remember_task", lambda *a: seen.append(a))
    monkeypatch.setattr(followups, "ensure_started", lambda: seen.append("started"))
    monkeypatch.setattr(turn.sync, "profile_lock", lambda _a: (_ for _ in ()).throw(RuntimeError("stop here")))
    identity = {"agentId": "ceo", "runId": "r1", "sessionKey": TASK_SESSION, "taskId": TASK, "runSecret": "s"}
    with pytest.raises(RuntimeError, match="stop here"):
        turn._run_task_turn(identity, {}, family="task")
    assert seen == [(TASK_SESSION, TASK, "s"), "started"]
    seen.clear()
    with pytest.raises(RuntimeError, match="stop here"):  # Paperclip/router/no-secret runs: untouched
        turn._run_task_turn({**identity, "sessionId": "p", "runSecret": None}, {}, family="task")
    with pytest.raises(RuntimeError, match="stop here"):
        turn._run_task_turn({**identity, "sessionId": "p"}, {}, family="paperclip")
    assert seen == []
