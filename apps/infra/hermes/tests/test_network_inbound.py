"""bridge.network's sweep: inbound network messages become local run-less turns, acked
`delivered` when the turn starts and `read` only when it returned; and the content-free hold
notice. Runs against test_network's fake sidecar; turn.run_turn is replaced by a recorder."""
from __future__ import annotations

import threading

import pytest

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
        finally:
            turn.release_turn()  # the real run_turn's _end_turn does this

    monkeypatch.setattr(turn, "run_turn", fake_run_turn)
    fake_run_turn.ran, fake_run_turn.gate = ran, gate
    return fake_run_turn


def _acks(n, stage=None):
    return [(c["id"], c["stage"]) for c in n.calls("inbox.ack") if stage is None or c["stage"] == stage]


def test_a_sweep_starts_one_turn_per_record_acked_delivered_at_start_and_read_at_end(net, turns):
    net.answer(**_peek(REC, {**REC, "id": "m2", "thread": "t-2"}))
    assert network.ensure_started(background=False)        # a sweep runs at start
    assert _until(lambda: len(_acks(net, "read")) == 2)
    assert sorted(b["eventId"] for _, b in turns.ran) == ["m1", "m2"]
    agent, body = next(t for t in turns.ran if t[1]["eventId"] == "m1")
    assert agent == "a-friday"
    assert body == {"version": 1, "eventKind": "network_message", "eventId": "m1", "targetAgentId": "a-friday",
                    "sessionKey": "t-1", "message": {"text": "[peer] hello", "label": "Prasiddha's Mac"}}
    for rid in ("m1", "m2"):
        stages = [s for i, s in _acks(net) if i == rid]
        assert stages == ["delivered", "read"]
    assert all(c["session"] == "friday" for c in net.calls("inbox.ack"))
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
    assert len(net.calls("inbox.peek")) == 3


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
