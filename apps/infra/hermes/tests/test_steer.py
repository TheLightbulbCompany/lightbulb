"""Task 10: steer_run. The HTTP steer route (the `steer` branch of _RUN_CONTROL_RE, including
the text-length/empty-text 400 checks) lives in bridge/server.py — lane B's file / the
controller's merge; see lane-A-report.md for the exact branch text."""
from bridge import turn

RUN = "22222222-2222-4222-8222-222222222222"


def _agent(accepted=True):
    class A:
        def __init__(self): self.seen = []
        def steer(self, text): self.seen.append(text); return accepted
    return A()


def test_steering_a_running_turn_queues_the_text():
    turn.RUNS.clear(); turn.register_run(RUN)
    agent = _agent(); turn.attach_agent(RUN, agent)
    assert turn.steer_run(RUN, "use staging") is True
    assert agent.seen == ["use staging"]


def test_a_queued_turn_has_nothing_to_steer():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.steer_run(RUN, "x") is False


def test_a_refused_steer_is_false():
    turn.RUNS.clear(); turn.register_run(RUN); turn.attach_agent(RUN, _agent(accepted=False))
    assert turn.steer_run(RUN, "x") is False


def test_an_unknown_run_is_false():
    turn.RUNS.clear()
    assert turn.steer_run(RUN, "x") is False


def test_steer_never_raises_even_if_the_agent_blows_up():
    turn.RUNS.clear(); turn.register_run(RUN)

    class Boom:
        def steer(self, text): raise RuntimeError("nope")

    turn.attach_agent(RUN, Boom())
    assert turn.steer_run(RUN, "x") is False
