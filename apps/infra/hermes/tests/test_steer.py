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


def test_a_queued_turn_keeps_the_steer_for_its_own_user_message():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.steer_run(RUN, "Ada: also check staging") is True
    assert turn.steer_run(RUN, "Ada: and prod") is True
    agent = _agent()
    assert turn.attach_agent(RUN, agent) == "Ada: also check staging\nAda: and prod"
    assert agent.seen == []  # delivered in the user message, not as a mid-turn steer


def test_an_aborted_or_finished_turn_refuses_so_the_backend_dispatches_its_own():
    turn.RUNS.clear(); turn.register_run(RUN); turn.attach_agent(RUN, _agent())
    assert turn.settle_steers(RUN, object(), None) is None  # nothing owed: stops taking steers
    assert turn.steer_run(RUN, "late") is False
    turn.RUNS.clear(); turn.register_run(RUN); turn.abort_run(RUN)
    assert turn.steer_run(RUN, "x") is False


def test_a_steer_that_landed_after_the_last_tool_batch_is_still_owed():
    turn.RUNS.clear(); turn.register_run(RUN)

    class Late:
        pending = "Ada: one more"
        def _drain_pending_steer(self):
            text, self.pending = self.pending, None
            return text

    agent = Late(); turn.attach_agent(RUN, agent)
    assert turn.settle_steers(RUN, agent, "Ada: first") == "Ada: first\nAda: one more"
    assert turn.RUNS[RUN]["state"] == "running"  # still steerable while it answers them
    assert turn.settle_steers(RUN, agent, None) is None
    assert turn.RUNS[RUN]["state"] == "finishing"


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


def test_a_leftover_steer_is_answered_in_the_same_run(home, posted, monkeypatch):
    from types import SimpleNamespace
    from tests import factories as f

    calls = []

    def run_conversation(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"api_calls": 1, "final_response": "first answer", "messages": ["m"], "pending_steer": "Ada: also X"}
        return {"api_calls": 1, "final_response": "about X"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: SimpleNamespace(run_conversation=run_conversation))
    turn.RUNS.clear()
    turn.run_turn("ceo", f.ingress(run={"runId": RUN, "workspaceId": f.ROOM, "frameChannel": "", "frameEndpoint": ""}))
    assert [c["user_message"] for c in calls[1:]] == ["Ada: also X"]
    assert calls[1]["conversation_history"] == ["m"]
    replies = [body for path, body in posted if "agent-messages" in path]
    assert replies and replies[0]["text"] == "first answer\n\nabout X"
    assert turn.RUNS[RUN]["state"] == "ended"
