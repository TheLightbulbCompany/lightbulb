"""bridge.network's sweep: inbound network messages become local run-less turns, acked
`delivered` when the turn starts and `read` only when it returned; and the content-free hold
notice. Runs against test_network's fake sidecar; turn.run_turn is replaced by a recorder."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import bridge
from bridge import network, turn
from test_network import _until, net  # noqa: F401 -- the fake sidecar fixture

REC = {"id": "m1", "from": "mac-prasiddha", "thread": "t-1", "framed": "[peer] hello", "label": "Prasiddha's Mac"}


def _peek(*records, session="friday"):
    return {"inbox.peek": {"sessions": [{"session": session, "records": list(records)}]}}


@pytest.fixture
def turns(monkeypatch):
    ran: list[tuple[str, dict]] = []
    gate = {"raise": False}

    def fake_run_turn(agent_id, body, token, reserved):
        assert reserved is True
        try:
            if gate["raise"]:
                raise RuntimeError("boom")
            ran.append((agent_id, body))
            return "completed"
        finally:
            turn.release_turn()  # the real run_turn's _end_turn does this

    monkeypatch.setattr(turn, "run_turn", fake_run_turn)
    fake_run_turn.ran, fake_run_turn.gate = ran, gate
    return fake_run_turn


def _acks(n, stage=None):
    return [(c["record"], c["stage"]) for c in n.calls("inbox.ack") if stage is None or c["stage"] == stage]


def test_a_sweep_starts_one_turn_per_record_acked_delivered_at_start_and_read_at_end(net, turns):
    net.answer(**_peek(REC, {**REC, "id": "m2", "thread": "t-2"}))
    assert network.ensure_started(background=False)        # a sweep runs at start
    assert _until(lambda: len(_acks(net, "read")) == 2)
    assert sorted(b["eventId"] for _, b in turns.ran) == ["m1", "m2"]
    agent, body = next(t for t in turns.ran if t[1]["eventId"] == "m1")
    assert agent == "a-friday"
    assert body == {"version": 1, "eventKind": "network_message", "eventId": "m1", "targetAgentId": "a-friday",
                    "sessionKey": "t-1", "abortKey": "event:m1",
                    "message": {"text": "[peer] hello", "label": "Prasiddha's Mac"}}
    for rid in ("m1", "m2"):
        stages = [s for i, s in _acks(net) if i == rid]
        assert stages == ["delivered", "read"]
    assert all(c["session"] == "friday" for c in net.calls("inbox.ack"))
    assert all(isinstance(c["id"], int) for c in net.calls("inbox.ack"))  # the line id is ours, never the record
    assert turn._busy == 0


def test_one_sender_thread_reuses_one_hermes_session(net, turns):
    net.answer(**_peek(REC, {**REC, "id": "m2"}))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(turns.ran) == 2)
    keys = {turn._resolve_session_key(a, b) for a, b in turns.ran}
    assert keys == {"agent:a-friday:lightbulb:network:t-1"}
    # No thread: the sender is the session, still never per event.
    assert turn._resolve_session_key("a-friday", {"eventKind": "network_message", "eventId": "x",
                                                  "sessionKey": REC["from"]}) == \
        "agent:a-friday:lightbulb:network:mac-prasiddha"
    assert turn._turn_user_message(turns.ran[0][1]) == "[peer] hello"


def test_a_recycle_hold_leaves_the_message_unacked(net, turns, monkeypatch):
    net.answer(**_peek(REC))
    real_reserve = turn.reserve_turn
    monkeypatch.setattr(turn, "reserve_turn", lambda: False)
    assert network.ensure_started(background=False)
    assert _until(lambda: net.calls("inbox.peek"))
    network.sweep()
    assert _acks(net) == [] and turns.ran == []
    monkeypatch.setattr(turn, "reserve_turn", real_reserve)  # the hold lifts
    network.sweep()                                          # the next sweep runs it
    assert _until(lambda: _acks(net) == [("m1", "delivered"), ("m1", "read")])


def test_a_turn_that_raises_is_not_acked_read(net, turns):
    turns.gate["raise"] = True
    net.answer(**_peek(REC))
    assert network.ensure_started(background=False)
    assert _until(lambda: _acks(net) == [("m1", "delivered")])
    network.sweep()                                          # once per process: no re-run, no read
    assert _acks(net) == [("m1", "delivered")]
    assert turn._busy == 0


def test_a_replayed_record_id_runs_once_per_process(net, turns):
    net.answer(**_peek(REC))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(turns.ran) == 1)
    network.sweep()
    network.sweep()
    assert len(turns.ran) == 1
    assert "event:m1" in turn.RUNS
    assert turn._busy == 0


def test_a_lagged_event_triggers_a_sweep(net, turns):
    assert network.ensure_started(background=False)
    assert _until(lambda: len(net.calls("inbox.peek")) == 1)
    net.answer(**_peek(REC))
    network._handle_event({"event": "lag"})
    assert _until(lambda: len(turns.ran) == 1)
    network._handle_event({"event": "message", "to_session": "friday", "id": "m9"})
    assert _until(lambda: len(net.calls("inbox.peek")) == 3)


def test_a_hold_notice_carries_no_text(net, monkeypatch):
    posted: list[tuple[str, dict]] = []
    done = threading.Event()

    def fake_post(path, body):
        posted.append((path, body))
        done.set()
        return 200

    monkeypatch.setattr(turn, "_post_backend", fake_post)
    net.answer(**{"hold.get": {"id": "H3", "kind": "message", "target_session": "friday", "from": "stranger",
                               "label": "Stranger (unverified)", "body": "SECRET BODY", "preview": "SECRET",
                               "state": "open"}})
    assert network.ensure_started(background=False)
    network._handle_event({"event": "held", "id": "H3", "target_session": "friday", "label": "Stranger"})
    assert done.wait(5)
    path, body = posted[0]
    assert path == "network/holds/org_test"
    assert set(body) == {"holdId", "state", "label", "target"}
    assert body == {"holdId": "H3", "state": "open", "label": "Stranger (unverified)", "target": "friday"}
    assert "SECRET" not in repr(posted)
    assert net.calls("hold.get")[-1]["hold"] == "H3"
    done.clear()
    network._handle_event({"event": "hold_closed", "id": "H3", "verdict": "allow", "by": "user_x"})
    assert done.wait(5)
    assert posted[1][1]["state"] == "closed" and set(posted[1][1]) == {"holdId", "state", "label", "target"}


def test_a_failed_real_turn_is_acked_delivered_but_not_read(net, monkeypatch):
    """The real run_turn reports a failure as outcome "error" and returns; it never raises."""
    from bridge import sync

    def broken_restore(agent_id):
        raise RuntimeError("bad S3 object")

    monkeypatch.setattr(turn, "restore_profile", broken_restore)
    monkeypatch.setattr(sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn, "save_profile", lambda agent_id: None)
    monkeypatch.setattr(turn, "report_usage", lambda *a, **kw: None)
    net.answer(**_peek(REC))
    assert network.ensure_started(background=False)
    assert _until(lambda: _acks(net) == [("m1", "delivered")] and turn._busy == 0)
    time.sleep(0.2)
    assert _acks(net) == [("m1", "delivered")]


def test_the_identity_line_names_the_agent_from_its_session_title(net):
    assert network.ensure_started(background=False)
    body = {"version": 1, "eventKind": "network_message", "eventId": "m1", "targetAgentId": "a-friday",
            "sessionKey": "t-1", "abortKey": "event:m1",
                    "message": {"text": "[peer] hello", "label": "Prasiddha's Mac"}}
    assert turn.system_prompt(body, "a-friday").startswith(
        "You are Friday, answering a network message from Prasiddha's Mac. "
        "Reply with lightbulb_network if a reply is needed.")
    assert turn.system_prompt({**body, "targetAgentId": "a-x"}, "a-x").startswith("You are an agent, ")


def test_an_assignment_record_carries_its_fields_into_the_turn(net, turns):
    net.answer(**_peek({**REC, "kind": "assign", "done_when": "tests pass", "deadline_at": "2026-10-30T00:00:00Z",
                        "reply_to": "r0", "status": "done"}))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(_acks(net, "read")) == 1)
    msg = turns.ran[0][1]["message"]
    assert msg == {"text": "[peer] hello", "label": "Prasiddha's Mac", "kind": "assign", "done_when": "tests pass",
                   "deadline_at": "2026-10-30T00:00:00Z", "reply_to": "r0", "status": "done"}


def _assign(net, turns, monkeypatch, reply, reply_tool=False):
    real = turn.run_turn

    def answering(agent_id, body, token, reserved):
        body["finalReply"] = reply
        if reply_tool:
            network.ask("send", {"from": "friday", "reply_to": "r1", "text": "by tool"})
        return real(agent_id, body, token, reserved)

    monkeypatch.setattr(turn, "run_turn", answering)
    network._replied.clear()
    net.answer(**_peek({**REC, "kind": "assign", "request_id": "r1"}), send={})
    assert network.ensure_started(background=False)
    assert _until(lambda: ("m1", "read") in _acks(net))
    return net.calls("send")


def test_an_assignment_answered_only_in_text_still_returns_its_result(net, turns, monkeypatch):
    sent = _assign(net, turns, monkeypatch, "391")
    assert [(c["reply_to"], c["text"], c["status"], c["from"]) for c in sent] == [("r1", "391", "done", "friday")]


def test_an_assignment_with_no_text_reports_failed(net, turns, monkeypatch):
    sent = _assign(net, turns, monkeypatch, "  ")
    assert [(c["status"], c["reason"]) for c in sent] == [("failed", "error")]


def test_an_assignment_the_agent_answered_is_not_answered_twice(net, turns, monkeypatch):
    sent = _assign(net, turns, monkeypatch, "391", reply_tool=True)
    assert [c["text"] for c in sent] == ["by tool"]


CANCEL = {**REC, "id": "c1", "kind": "cancel", "reply_to": "m1"}


def test_a_cancel_stops_the_running_turn_without_a_model_turn(net, turns):
    turn.discard_run("event:m1")
    turn.register_run("event:m1")
    net.answer(**_peek(CANCEL), send={})
    assert network.ensure_started(background=False)
    assert _until(lambda: ("c1", "read") in _acks(net))
    assert turn.run_aborted("event:m1") and not turns.ran
    assert [(c["reply_to"], c["status"]) for c in net.calls("send")] == [("m1", "cancelled")]
    turn.discard_run("event:m1")


def test_a_cancel_after_the_turn_ended_sends_nothing(net, turns):
    turn.discard_run("event:m1")
    turn.register_run("event:m1")
    turn.finish_run("event:m1", "completed")
    net.answer(**_peek(CANCEL), send={})
    assert network.ensure_started(background=False)
    assert _until(lambda: ("c1", "read") in _acks(net))
    assert net.calls("send") == []
    turn.discard_run("event:m1")


def test_an_assignment_whose_turn_fails_tells_the_asker_and_is_not_run_again(net, monkeypatch):
    def failed(agent_id, body, token, reserved):
        turn.release_turn()
        return "error"

    monkeypatch.setattr(turn, "run_turn", failed)
    network._replied.clear()
    net.answer(**_peek({**REC, "kind": "assign", "request_id": "r1"}), send={})
    assert network.ensure_started(background=False)
    assert _until(lambda: ("m1", "read") in _acks(net))
    assert [(c["reply_to"], c["status"], c["reason"]) for c in net.calls("send")] == [("r1", "failed", "error")]


def test_an_assignment_turn_is_stopped_at_its_deadline(net, monkeypatch):
    from datetime import datetime, timedelta, timezone

    def slow(agent_id, body, token, reserved):
        t0 = time.time()
        while not turn.run_aborted(body["abortKey"]) and time.time() - t0 < 5:
            time.sleep(0.02)
        turn.release_turn()
        return "aborted" if turn.run_aborted(body["abortKey"]) else "completed"

    monkeypatch.setattr(turn, "run_turn", slow)
    soon = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
    net.answer(**_peek({**REC, "kind": "assign", "request_id": "r1", "deadline_at": soon}), send={})
    t0 = time.time()
    assert network.ensure_started(background=False)
    assert _until(lambda: ("m1", "read") in _acks(net))
    assert time.time() - t0 < 3
    assert net.calls("send") == []  # the asker's engine already said timed_out


def test_seconds_left_reads_both_deadline_forms():
    assert network._seconds_left(time.time() + 10) > 9
    assert network._seconds_left("2020-01-01T00:00:00Z") < 0
    assert network._seconds_left(None) is None and network._seconds_left("soon") is None


def test_a_cancelled_assignment_is_read_and_sends_no_result(net, monkeypatch):
    def stopped(agent_id, body, token, reserved):
        turn.abort_run(body["abortKey"])
        body["finalReply"] = "half done"
        turn.release_turn()
        return "aborted"

    monkeypatch.setattr(turn, "run_turn", stopped)
    net.answer(**_peek({**REC, "kind": "assign", "request_id": "r1"}), send={})
    assert network.ensure_started(background=False)
    assert _until(lambda: ("m1", "read") in _acks(net))
    assert net.calls("send") == []


def test_the_identity_line_for_an_assignment(net):
    assert network.ensure_started(background=False)
    body = {"version": 1, "eventKind": "network_message", "eventId": "m1", "targetAgentId": "a-friday",
            "sessionKey": "t-1", "message": {"text": "x", "label": "Mac", "kind": "assign"}}
    assert turn.system_prompt(body, "a-friday").startswith(
        "You are Friday. This is an assignment from Mac: do it, then reply with lightbulb_network action send, "
        "reply_to its requestId, and status failed or cancelled if you could not finish.")


def test_network_workers_is_written_only_when_the_runtime_asks(net, monkeypatch):
    cfg = network.NET_HOME / "config.toml"
    monkeypatch.delenv("LIGHTBULB_NETWORK_WORKERS", raising=False)
    network._enable_workers()
    assert not cfg.exists()
    network.NET_HOME.mkdir(parents=True, exist_ok=True)
    cfg.write_text('name = "x"\nnetwork_workers = false\n')
    network._enable_workers()
    assert cfg.read_text() == 'name = "x"\nnetwork_workers = false\n'  # off: untouched
    monkeypatch.setenv("LIGHTBULB_NETWORK_WORKERS", "1")
    network._enable_workers()
    network._enable_workers()
    assert cfg.read_text() == 'name = "x"\nnetwork_workers = true\n'


def test_network_workers_goes_above_the_first_table(net, monkeypatch):
    monkeypatch.setenv("LIGHTBULB_NETWORK_WORKERS", "1")
    network.NET_HOME.mkdir(parents=True, exist_ok=True)
    cfg = network.NET_HOME / "config.toml"
    cfg.write_text('# mine\nname = "x"\nnetwork_workers = false\n[t]\nnetwork_workers = 5\n', encoding="utf-8")
    network._enable_workers()
    assert cfg.read_text(encoding="utf-8") == '# mine\nname = "x"\nnetwork_workers = true\n[t]\nnetwork_workers = 5\n'


@pytest.fixture
def backend(monkeypatch):
    """``urlopen`` to the backend: records each POST and answers ``status["code"]``."""
    monkeypatch.setattr(bridge, "API", "https://api.test")
    monkeypatch.setattr(bridge, "HOOK_TOKEN", "hook-tok")
    posted: list = []
    status = {"code": 503}

    class _Res:
        def __init__(self, code):
            self.status = code

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posted.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        if status["code"] >= 300:
            raise urllib.error.HTTPError(req.full_url, status["code"], "no", {}, None)
        return _Res(status["code"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return posted, status


A2A_SESSION = "a2a-1a2b3c4d"
A2A_REPLY = {"id": "m9", "from": "mac-p", "reply_to": "task-1", "text": "all green", "framed": "[Lightbulb] all green"}


def test_a_reply_to_an_a2a_session_goes_to_the_backend_not_a_turn(net, turns, backend):
    posted, status = backend
    net.answer(**_peek(A2A_REPLY, session=A2A_SESSION))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(posted) == 1)
    for code in (401, 429):  # retried, never acked
        status["code"] = code
        network.sweep()
        assert _until(lambda: len(posted) >= 2)
    time.sleep(0.2)
    assert _acks(net) == []  # a 5xx, 401 or 429 is left unacked for the next sweep
    status["code"] = 200
    network.sweep()
    assert _until(lambda: len(_acks(net, "read")) == 1)
    url, headers, body = posted[-1]
    assert url == "https://api.test/api/v1/internal/a2a/org_test/session-reply"
    assert headers["Authorization"] == "Bearer hook-tok"
    assert body == {"request_id": "task-1", "text": "all green", "session": A2A_SESSION, "from": "mac-p"}
    assert _acks(net) == [("m9", "delivered"), ("m9", "read")]
    assert all(c["session"] == A2A_SESSION for c in net.calls("inbox.ack"))
    assert turns.ran == []


@pytest.mark.parametrize("code", [404, 422])
def test_an_unknown_or_bad_a2a_reply_is_acked(net, turns, backend, code):
    posted, status = backend
    status["code"] = code
    net.answer(**_peek(A2A_REPLY, session=A2A_SESSION))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(_acks(net, "read")) == 1)
    assert len(posted) >= 1


def test_an_a2a_record_without_reply_to_is_acked_unforwarded(net, turns, backend):
    posted, _ = backend
    net.answer(**_peek({k: v for k, v in A2A_REPLY.items() if k != "reply_to"}, session=A2A_SESSION))
    assert network.ensure_started(background=False)
    assert _until(lambda: len(_acks(net, "read")) == 1)
    assert posted == [] and turns.ran == []


def test_concurrent_a2a_declares_keep_both_people(net):
    assert network.ensure_started(background=False)
    threads = [threading.Thread(target=network.declare_a2a_session, args=(p,)) for p in ("A", "B", "C", "D")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    last = net.calls("cloud.sessions.set")[-1]["sessions"]
    assert {s["person"] for s in last if s.get("app") == "a2a"} == {"A", "B", "C", "D"}
