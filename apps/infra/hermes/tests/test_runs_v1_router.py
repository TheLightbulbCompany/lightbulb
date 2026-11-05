"""Router mode on /internal/v1/runs (wave-2 O Task 4): one session per lane, decisions only."""
import pytest

from bridge import runs, server, turn
from tests.test_runs_v1 import AGENT, AUTH, CREATE, REQUEST, TASK, Agent, quiet_turn, threads  # noqa: F401

RUN = "1c6d7e40-5b6c-4d7e-8f90-a1b2c3d4e5f6"
LANE = "shared"
KEY = f"agent:{AGENT}:lightbulb:router:{LANE}"
IDENTITY = {"runId": RUN, "ownerId": "org_test", "taskId": TASK, "attempt": 1, "agentId": AGENT,
            "sessionKey": KEY, "mode": "router", "lane": LANE, "session": {"kind": "workspace"}}


def _post(identity=IDENTITY):
    return server.handle_plugin(CREATE, "POST", AUTH, {"identity": identity, "request": REQUEST})


def _poll():
    return server.handle_plugin(f"{CREATE}/{RUN}", "GET", AUTH, {"identity": IDENTITY})


class RouterAgent(Agent):
    def run_conversation(self, **kw):
        self.calls.append(kw)
        turn.record_decision(RUN, {"kind": "start_task", "triggerIds": ["chat:m1"], "title": "Pay",
                                   "brief": "Pay Acme", "ack": "On it"})
        turn.record_decision(RUN, {"kind": "reply", "triggerIds": ["chat:m2"], "text": "Done already."})
        return {"api_calls": 1, "final_response": "ok"}


def test_router_run_returns_decisions(quiet_turn, threads, monkeypatch, run_events):  # noqa: F811
    built = {}

    def construct(agent_id, body, frames, session_key, **limits):
        built.update(limits, session_key=session_key)
        return RouterAgent()

    monkeypatch.setattr(turn, "_construct_agent", construct)
    assert _post()[0] == 202
    threads[0].run_now()
    code, status = _poll()
    assert code == 200 and status["status"] == "completed"
    assert [d["kind"] for d in status["decisions"]] == ["start_task", "reply"]
    assert built["session_key"] == KEY
    assert run_events == []  # router_turn polls; only Worker runs post run events


def test_router_agent_has_exact_tool_surface(quiet_turn, threads, monkeypatch):  # noqa: F811
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    _post()
    threads[0].run_now()
    assert built["enabled_toolsets"] == ["lightbulb_router", "memory"]
    assert built["disabled_toolsets"] is None


def test_router_skips_background_review(quiet_turn, threads, monkeypatch):  # noqa: F811
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    _post()
    threads[0].run_now()
    assert built["skip_background_review"] is True


def test_router_never_side_sessions(quiet_turn, threads, monkeypatch):  # noqa: F811
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    monkeypatch.setattr(turn, "_side_session", lambda *a, **k: pytest.fail("side session in a Router run"))
    _post()
    threads[0].run_now()
    assert _poll()[1]["status"] == "completed"


@pytest.mark.parametrize("bad", [
    {**IDENTITY, "lane": "u:abc"},                                            # malformed lane
    {**IDENTITY, "sessionKey": f"agent:{AGENT}:lightbulb:router:u:0123456789abcdef"},  # key names another lane
    {**IDENTITY, "sessionKey": f"agent:{AGENT}:lightbulb:task:{TASK}"},       # a Worker key in Router mode
])
def test_router_identity_validated(threads, bad):  # noqa: F811
    assert _post(bad)[0] == 400


def test_record_decision_only_for_router_runs(threads):  # noqa: F811
    _post()
    assert turn.record_decision(RUN, {"kind": "ignore", "triggerIds": ["chat:m1"]}) is True
    assert turn.record_decision("not-a-run", {"kind": "ignore"}) is False


def test_router_session_key_pattern_matches_schema():
    from tests.test_runs_v1 import _schema_path
    import json

    path = _schema_path()
    pattern = path and (json.loads(path.read_text()).get("sessionKeys") or {}).get("router")
    assert pattern == runs.ROUTER_SESSION_KEY_RE.pattern


def test_router_uses_the_model_the_backend_chose(quiet_turn, threads, monkeypatch):  # noqa: F811
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    _post({**IDENTITY, "model": "claude-haiku-4-5"})
    threads[0].run_now()
    assert built["model"] == "claude-haiku-4-5"
