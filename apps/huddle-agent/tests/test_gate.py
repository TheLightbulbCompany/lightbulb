from huddle_agent import gate


def test_no_kill_switch():
    assert not hasattr(gate, "enabled")  # no flags: LIGHTBULB_PROACTIVE is gone
