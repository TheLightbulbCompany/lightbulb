"""effects: "required" on a v1 run (wave2-Q Task 5 wiring): refused without F1; with F1 the effects callback is
installed on the Worker's agent, and a turn that needs a person ends `waiting_approval` (poll + terminal event)."""
# ruff: noqa: F811 -- `quiet_turn` and `threads` are test_runs_v1's fixtures, imported and then requested by name.
import json
import sys

import pytest

from bridge import effects, runs, turn
from tests import fake_lb_checkpoint
from tests.test_runs_v1 import IDENTITY, REQUEST, RUN, TASK, Agent, _poll, _post, quiet_turn, threads  # noqa: F401

REQUIRED = {**REQUEST, "effects": "required"}


@pytest.fixture
def f1(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.lb_checkpoint", fake_lb_checkpoint)


@pytest.fixture
def events(monkeypatch):
    posted = []
    monkeypatch.setattr(runs, "_post_event", lambda identity, body: posted.append((identity, body)) or 200)
    monkeypatch.setattr(runs, "API", "https://api.example")
    return posted


def test_required_without_f1_is_unsupported(threads):
    assert _post({"identity": IDENTITY, "request": REQUIRED}) == (400, {"error": "unsupported_in_this_version"})
    assert threads == []


def test_bad_effects_or_faults_are_invalid(threads, f1):
    assert _post({"identity": IDENTITY, "request": {**REQUEST, "effects": "maybe"}})[0] == 400
    assert _post({"identity": IDENTITY, "request": {**REQUIRED, "faults": "kill"}})[0] == 400
    assert _post({"identity": IDENTITY, "request": {**REQUIRED, "faults": [1]}})[0] == 400
    assert threads == []


def test_required_installs_the_callback_with_the_runs_identity(quiet_turn, threads, f1, monkeypatch):
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    code, _ = _post({"identity": {**IDENTITY, "runSecret": "s3"}, "request": {**REQUIRED, "faults": ["kill_after_effect_intent"]}})
    assert code == 202
    threads[0].run_now()
    cb = agent._lb_checkpoint
    assert isinstance(cb, effects.EffectsCallback)
    assert (cb.task_id, cb.attempt, cb.run_id, cb.run_secret, cb.agent_id) == (TASK, 1, RUN, "s3", IDENTITY["agentId"])
    assert cb.faults == {"kill_after_effect_intent"}
    assert _poll()[1]["status"] == "completed"


def test_off_installs_nothing(quiet_turn, threads, f1, monkeypatch):
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    assert _post()[0] == 202
    threads[0].run_now()
    assert getattr(agent, "_lb_checkpoint", None) is None


class WaitingAgent(Agent):
    """The Worker's turn hits an effect the backend holds for a person: the callback refused it and set the outcome."""

    def run_conversation(self, **kw):
        self._lb_checkpoint.outcome = "waiting_approval"
        return super().run_conversation(**kw)


def test_needs_approval_ends_the_turn_waiting_approval(quiet_turn, threads, f1, events, monkeypatch):
    agent = WaitingAgent(reply="I need Sam's OK to send the invoice.")
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    assert _post({"identity": IDENTITY, "request": REQUIRED})[0] == 202
    threads[0].run_now()
    code, status = _poll()
    assert code == 200 and status["status"] == "waiting_approval" and status["output"].startswith("I need Sam")
    assert "error" not in status
    [(_, terminal)] = [e for e in events if e[1]["kind"] == "terminal"]
    assert terminal["status"] == "waiting_approval" and terminal["output"].startswith("I need Sam")


def test_ask_marker_ends_the_turn_waiting_approval(quiet_turn, threads, f1, events, monkeypatch, home):
    """ask_person with `blocked` on a task run leaves the marker; the turn ends waiting_approval and the marker is taken."""
    agent = Agent(reply="Asked Sam which account.")

    def construct(*a, **k):
        path = turn._waiting_marker_path(turn._profile_dir(IDENTITY["agentId"]), RUN)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"questionId": "q-1"}))
        return agent

    monkeypatch.setattr(turn, "_construct_agent", construct)
    assert _post({"identity": IDENTITY, "request": REQUIRED})[0] == 202
    threads[0].run_now()
    assert _poll()[1]["status"] == "waiting_approval"
    assert not turn._waiting_marker_path(turn._profile_dir(IDENTITY["agentId"]), RUN).exists()


def test_router_runs_get_no_checkpoint(quiet_turn, threads, f1, monkeypatch):
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    router = {**IDENTITY, "mode": "router", "lane": "shared",
              "sessionKey": f"agent:{IDENTITY['agentId']}:lightbulb:router:shared"}
    code, _ = _post({"identity": router, "request": REQUIRED})
    if code == 202:
        threads[0].run_now()
        assert getattr(agent, "_lb_checkpoint", None) is None
