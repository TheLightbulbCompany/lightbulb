"""Turns always admit (Task 11): the provider-switch generation header and the switch-frozen
gate were retired -- a provider switch is now just a profile rewrite the bridge picks up on
its next turn (per-turn `.revision` check), not a flag the bridge enforces. What remains here
is N-1 compatibility with an OLD backend that still sends the stale header, plus the ordinary reserve/release admission
accounting (busy count, reservation released on a failed accept).
"""

import threading

import pytest

from bridge import server, turn
from tests import factories as f
from tests.test_paperclip_gateway import BODY, CREATE_PATH

AUTH = {"authorization": "Bearer gw-test"}


def test_reservation_and_release_are_atomic_under_concurrent_arrival():
    outcomes = []
    start = threading.Barrier(2)

    def admit():
        start.wait()
        outcomes.append(turn.reserve_turn())

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes == [True, True]
    assert turn._busy == 2
    turn.release_turn()
    turn.release_turn()


def test_stale_or_missing_generation_header_is_admitted(monkeypatch, posted):
    """An old backend may still send X-Lightbulb-Provider-Generation (stale, or naming a
    generation this process never set) -- the bridge ignores the header entirely now."""
    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    stale = {**AUTH, "X-Lightbulb-Provider-Generation": "gen-old"}
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", stale, f.ingress())[0] == 202
    turn.release_turn()
    # No header at all -- the ordinary case now that the backend never sends one.
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY)[0] == 202
    turn.release_turn()


def test_provider_switch_frozen_boot_secret_no_longer_blocks_turns(monkeypatch, posted):
    """A boot secret with providerSwitchFrozen=true (an old backend's shape) does nothing --
    there is no `_turns_frozen` flag left to set, so a turn is admitted regardless."""
    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    assert not hasattr(turn, "_turns_frozen")
    assert server.handle_plugin("/plugins/lightbulb-channel/inbound/ceo", "POST", AUTH, f.ingress())[0] == 202
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
