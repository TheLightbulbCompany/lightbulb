"""Spec 13 §8.3: a long turn's brokered grants are re-vended before they expire (V11)."""

import threading
import time

from bridge import credentials, turn


def _last(agent, **expires):
    credentials._LAST[agent] = credentials.Vend(tokens={"x": "XT"}, expires_at=expires)


def test_renews_under_10_min():
    now = 1_000_000.0
    revends = []
    r = credentials.TurnRenewer("friday", clock=lambda: now, revend=revends.append)

    _last("friday", x=int(now) + 601)
    assert r.tick() is False and revends == []
    _last("friday", x=int(now) + 599)
    assert r.tick() is True and revends == ["friday"]
    credentials._LAST["friday"] = credentials.Vend(tokens={"stripe": "s"}, expires_at={"stripe": int(now)})
    assert r.tick() is False  # only brokered grants count: an MCP bearer is re-vended per turn anyway


def test_renewer_thread_wakes_and_revends():
    _last("friday", x=int(time.time()) + 10)
    revends = []
    r = credentials.TurnRenewer("friday", wake_s=0.01, revend=revends.append).start()
    deadline = time.time() + 2
    while not revends and time.time() < deadline:
        time.sleep(0.01)
    r.stop()
    assert revends and revends[0] == "friday"


def test_stops_with_scope():
    _last("friday", x=int(time.time()) + 3600)
    with turn._brokered_mcp_scope({}, "friday"):
        live = [t for t in threading.enumerate() if t.name.startswith("renew-")]
        assert len(live) == 1
    live[0].join(timeout=2)
    assert not live[0].is_alive()  # no thread leak

    credentials._LAST.clear()
    with turn._brokered_mcp_scope({}, "friday"):  # nothing brokered: no renewer at all
        assert not [t for t in threading.enumerate() if t.name.startswith("renew-")]
