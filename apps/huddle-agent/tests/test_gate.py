from huddle_agent import gate
from huddle_agent.gate import RateLimit


def test_rate_limit_one_per_90s_and_ten_per_hour():
    r = RateLimit()
    assert r.ok(0)
    r.record(0)
    assert not r.ok(gate.MIN_GAP - 1) and r.ok(gate.MIN_GAP)
    for i in range(1, 10):
        r.record(i * gate.MIN_GAP)
    assert not r.ok(10 * gate.MIN_GAP)  # ten in the last hour
    assert r.ok(3600)  # the first one aged out


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("LIGHTBULB_PROACTIVE", raising=False)
    assert gate.enabled()
    monkeypatch.setenv("LIGHTBULB_PROACTIVE", "OFF")
    assert not gate.enabled()
