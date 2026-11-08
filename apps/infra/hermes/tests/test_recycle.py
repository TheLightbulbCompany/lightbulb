"""The 7 h recycle: stop the session at an idle moment before AgentCore's 8 h maxLifetime kills
it mid-turn (prod 2026-09-25 21:00:04), under a self-expiring hold that refuses new turns."""
import bridge
import os
import shutil
import time
from types import SimpleNamespace

import pytest

from bridge import recycle, server, sync, turn

_disk_free_mb = sync.disk_free_mb
from tests import factories as f
from tests.test_paperclip_gateway import BODY, CREATE_PATH

AUTH = {"authorization": "Bearer gw-test"}
INBOUND = "/plugins/lightbulb-channel/inbound/ceo"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    turn.release_hold()
    monkeypatch.setattr(recycle, "first_invoke_at", None)
    monkeypatch.setattr(recycle, "_next_try", 0.0)
    monkeypatch.setattr(sync, "disk_free_mb", lambda: (10**6, 10**6))  # a full laptop is not a trigger
    yield
    turn.release_hold()


@pytest.fixture
def stops(monkeypatch):
    calls, answer = [], {"status": 202}
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: calls.append(path) or answer["status"])
    return calls, answer


def _aged(monkeypatch, seconds):
    monkeypatch.setattr(recycle, "first_invoke_at", time.time() - seconds)


def test_the_clock_starts_at_the_first_invoke_not_at_boot(stops):
    assert recycle.check_once() is False  # a warm-pool microVM: never invoked, never recycled
    recycle.first_invoke_at = time.time() - recycle.RECYCLE_AFTER_S + 60
    assert recycle.check_once() is False
    assert stops[0] == []


def test_an_idle_session_past_the_threshold_asks_for_a_stop_under_a_hold(stops, monkeypatch):
    _aged(monkeypatch, recycle.RECYCLE_AFTER_S + 1)
    assert recycle.check_once() is True
    assert stops[0] == [f"runtime/{bridge.OWNER}/recycle"]
    assert turn.reserve_turn() is False  # refusing while the stop lands


def test_a_busy_session_waits_for_idle(stops, monkeypatch):
    _aged(monkeypatch, recycle.RECYCLE_AFTER_S + 1)
    turn._busy_add(1)
    assert recycle.check_once() is False
    assert stops[0] == []
    turn._busy_add(-1)
    assert turn.reserve_turn() is True  # no hold was left behind
    turn.release_turn()


def test_the_hard_fallback_stops_even_a_busy_session(stops, monkeypatch):
    _aged(monkeypatch, recycle.RECYCLE_HARD_S + 1)
    turn._busy_add(1)
    assert recycle.check_once() is True
    assert stops[0]


def test_a_failed_stop_re_admits_turns(stops, monkeypatch):
    _aged(monkeypatch, recycle.RECYCLE_AFTER_S + 1)
    stops[1]["status"] = 503
    assert recycle.check_once() is False
    assert turn.reserve_turn() is True
    turn.release_turn()


def test_a_confirmed_stop_that_left_us_alive_is_not_repeated_at_once(stops, monkeypatch):
    _aged(monkeypatch, recycle.RECYCLE_AFTER_S + 1)
    assert recycle.check_once() is True
    turn.release_hold()
    assert recycle.check_once() is False
    assert len(stops[0]) == 1


def test_the_hold_expires_by_itself(monkeypatch):
    monkeypatch.setattr(turn, "HOLD_TTL_S", 0.05)
    assert turn.hold_turns() is True
    assert turn.reserve_turn() is False
    time.sleep(0.1)
    assert turn.reserve_turn() is True
    turn.release_turn()


def test_a_held_bridge_refuses_room_ingress_and_paperclip_creates_with_503(home):
    assert turn.hold_turns() is True
    assert server.handle_plugin(INBOUND, "POST", AUTH, f.ingress()) == (503, {"error": "recycling"})
    assert server.handle_plugin(CREATE_PATH, "POST", AUTH, BODY) == (503, {"error": "recycling"})
    assert turn._busy == 0


def test_health_drain_holds_an_idle_session_for_the_roll(home):
    code, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH,
                                     {"method": "health", "params": {"drain": True}})
    assert obj["payload"]["busy"] == 0
    assert turn.reserve_turn() is False


def test_health_drain_on_a_busy_session_holds_nothing(home):
    turn._busy_add(1)
    code, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH,
                                     {"method": "health", "params": {"drain": True}})
    assert obj["payload"]["busy"] == 1
    turn._busy_add(-1)
    assert turn.reserve_turn() is True
    turn.release_turn()


@pytest.mark.parametrize("outer_mb, inner_mb, stopped, forced", [
    (5000, 900, True, False),   # inner under the soft floor: idle-only
    (400, 5000, True, True),    # outer under the hard floor: stops even a busy session
    (5000, 5000, False, False),  # above both floors, young session: untouched
])
def test_low_disk_recycles_on_the_lower_of_outer_and_inner(stops, monkeypatch, outer_mb, inner_mb, stopped, forced):
    monkeypatch.setattr(sync, "disk_free_mb", _disk_free_mb)
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(f_bavail=outer_mb, f_frsize=1_000_000))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=inner_mb * 1_000_000))
    _aged(monkeypatch, 60)
    assert sync.disk_free_mb() == (outer_mb, inner_mb)
    turn._busy_add(1)
    assert recycle.check_once() is forced  # busy: only the hard floor stops it
    turn._busy_add(-1)
    if not forced:
        assert recycle.check_once() is stopped  # idle
    assert len(stops[0]) == int(stopped)
