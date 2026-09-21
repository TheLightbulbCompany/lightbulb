"""A switch freezes admission and drains turns already accepted by either ingress."""

import threading

import pytest

from bridge import server, turn
from tests import factories as f
from tests.test_paperclip_gateway import BODY, CREATE_PATH

AUTH = {"authorization": "Bearer gw-test"}


def test_reservation_and_freeze_are_atomic_under_concurrent_arrival():
    outcomes = []
    start = threading.Barrier(3)

    def admit():
        start.wait()
        outcomes.append(("admit", turn.reserve_turn()))

    def freeze():
        start.wait()
        outcomes.append(("freeze", turn.freeze_turns()))

    first = threading.Thread(target=admit)
    second = threading.Thread(target=freeze)
    first.start()
    second.start()
    start.wait()
    first.join()
    second.join()
    accepted = next(value for kind, value in outcomes if kind == "admit")
    busy_at_freeze = next(value for kind, value in outcomes if kind == "freeze")
    assert busy_at_freeze == (1 if accepted else 0)
    assert turn.reserve_turn() is False
    if accepted:
        turn.release_turn()


def test_room_and_native_paperclip_reserve_before_spawning(monkeypatch, posted):
    started_busy = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs["target"]
            self.args = kwargs["args"]

        def start(self):
            started_busy.append(turn._busy)

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    room = f.ingress()
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, room)[0] == 202
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    assert started_busy == [1, 2]
    assert turn.freeze_turns() == 2
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, f.ingress())[0] == 503
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 503
    turn.release_turn()
    turn.release_turn()


def test_freeze_rpc_sees_a_reserved_turn_before_its_thread_starts(monkeypatch, posted):
    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    room = f.ingress()
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, room)[0] == 202
    status, response = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH,
                                            {"method": "provider.freeze", "params": {}})
    assert status == 200
    assert response["payload"] == {"frozen": True, "busy": 1}
    turn.release_turn()


def test_delayed_pre_switch_turn_is_rejected_after_cold_thaw(monkeypatch, posted):
    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    monkeypatch.setenv("LIGHTBULB_PROVIDER_SWITCH_GENERATION", "gen-new")
    old = {**AUTH, "X-Lightbulb-Provider-Generation": "gen-old"}
    new = {**AUTH, "X-Lightbulb-Provider-Generation": "gen-new"}
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", old, f.ingress())[0] == 503
    assert server.handle_plugin(CREATE_PATH, "POST", old, BODY)[0] == 503
    assert turn._busy == 0
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", new, f.ingress())[0] == 202
    turn.release_turn()


def test_room_accept_failure_releases_reservation_and_allows_retry(monkeypatch):
    body = f.ingress()
    run_id = body["run"]["runId"]
    monkeypatch.setattr(turn, "_post_backend", lambda *a: (_ for _ in ()).throw(RuntimeError("callback down")))
    with pytest.raises(RuntimeError, match="callback down"):
        server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, body)
    assert turn._busy == 0
    assert run_id not in turn.RUNS


def test_paperclip_registration_failure_releases_reservation(monkeypatch):
    monkeypatch.setattr(turn, "register_paperclip_run",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("registry down")))
    with pytest.raises(RuntimeError, match="registry down"):
        server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)
    assert turn._busy == 0


def test_thread_start_failure_releases_once_and_forgets_run(monkeypatch, posted):
    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(server.threading, "Thread", BrokenThread)
    body = f.ingress()
    with pytest.raises(RuntimeError, match="thread unavailable"):
        server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, body)
    assert turn._busy == 0
    assert body["run"]["runId"] not in turn.RUNS
