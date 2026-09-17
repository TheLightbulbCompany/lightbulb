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
