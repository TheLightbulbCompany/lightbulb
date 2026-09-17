"""Task 9: the run registry + abort_run. The abort HTTP route itself lives in
bridge/server.py (lane B's file / the controller's merge) — see lane-A-report.md for the
exact route branch text these turn-side functions are meant to sit behind."""
from bridge import turn
from tests import factories as f

RUN = "22222222-2222-4222-8222-222222222222"


def test_aborting_a_queued_turn_marks_it_without_touching_an_agent():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.abort_run(RUN) is True
    assert turn.RUNS[RUN]["aborted"] is True and turn.RUNS[RUN]["agent"] is None


def test_aborting_a_running_turn_hard_interrupts_the_agent():
    turn.RUNS.clear(); turn.register_run(RUN)
    calls = []
    turn.attach_agent(RUN, type("A", (), {"interrupt": lambda self, **kw: calls.append(kw) or True})())
    assert turn.abort_run(RUN) is True
    assert calls == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]


def test_an_unregistered_or_already_ended_run_cannot_be_aborted():
    turn.RUNS.clear()
    assert turn.abort_run(RUN) is False
    turn.register_run(RUN)
    turn.finish_run(RUN, "completed")
    assert turn.abort_run(RUN) is False


def test_a_turn_aborted_before_it_launched_reports_zero_usage(home, posted, monkeypatch):
    turn.RUNS.clear()
    body = f.ingress()
    turn.register_run(body["run"]["runId"])
    turn.abort_run(body["run"]["runId"])
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    turn.run_turn("ceo", body)
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted" and ended["usage"] == turn.ZERO_USAGE


def test_a_normal_turn_still_registers_and_finishes_in_the_registry(home, posted, monkeypatch):
    turn.RUNS.clear()
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    body = f.ingress()
    turn.run_turn("ceo", body)
    assert turn.RUNS[body["run"]["runId"]]["state"] == "ended"
    assert turn.RUNS[body["run"]["runId"]]["outcome"] == "completed"


# ---- review fix (IMPORTANT 2): mid-flight abort ----------------------------------------------
# The pre-launch check (test above) only catches an abort that lands before _run_hermes is
# ever called. A run already IN _run_hermes when abort_run fires must also end up "aborted",
# and the tokens it already spent must not be thrown away.
def test_a_mid_flight_abort_after_the_agent_is_attached_overrides_the_outcome_but_keeps_usage(home, posted, monkeypatch):
    turn.RUNS.clear()
    body = f.ingress()
    run_id = body["run"]["runId"]
    spent_usage = {**turn.ZERO_USAGE, "inputTokens": 42}
    interrupted = []

    class FakeAgent:
        def interrupt(self, **kw):
            interrupted.append(kw)
            return True

    def fake_run_hermes(agent_id, body, frames):
        turn.attach_agent(run_id, FakeAgent())
        turn.abort_run(run_id)  # a concurrent caller stops the run while it is "running"
        return "completed", None, "partial reply that should be discarded", spent_usage

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", body)

    assert interrupted == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted" and ended["usage"] == spent_usage  # tokens were spent
    assert not any(p.startswith("agent-messages/") for p, _b in posted)  # the reply is discarded


def test_an_abort_landing_before_attach_agent_is_still_caught_by_the_post_check(home, posted, monkeypatch):
    """TOCTOU: abort_run can land in the queued window between run_turn's pre-launch check
    and _construct_agent/attach_agent ever running, with no live agent to interrupt. The
    post-check after _run_hermes returns must still catch it via RUNS[run_id]['aborted'],
    which abort_run sets regardless of whether an agent was attached."""
    turn.RUNS.clear()
    body = f.ingress()
    run_id = body["run"]["runId"]

    def fake_run_hermes(agent_id, body, frames):
        assert turn.RUNS[run_id]["agent"] is None  # never attached in this simulated race
        assert turn.abort_run(run_id) is True  # still succeeds with no agent to interrupt
        return "completed", None, "reply that should be discarded", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", body)
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted"
    assert not any(p.startswith("agent-messages/") for p, _b in posted)
