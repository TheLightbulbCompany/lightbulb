"""Review fix (CRITICAL 1): the Hermes home override must hold for AIAgent construction AND
the whole run_conversation, not just construction -- get_hermes_home() is a ContextVar read
at call time, and run_conversation-time writers (learning_graph, curator, skill_utils,
prompt_builder) resolve it while the turn is running. Hermes's own gateway holds the scope
"per profile activity, not per turn" (gateway/AGENTS.md:157-168)."""
import sys
import types
from contextlib import contextmanager, nullcontext

from bridge import turn
from tests import factories as f


class _BrowserAgent:
    model = "model"
    session_input_tokens = session_output_tokens = 0
    session_cache_read_tokens = session_cache_write_tokens = 0

    def run_conversation(self, **_kwargs):
        return {"api_calls": 1, "final_response": "ok"}


def _assert_browser_release_uses_profile_scope(monkeypatch, run):
    active = {"agent": None}

    @contextmanager
    def home_scope(agent_id):
        active["agent"] = agent_id
        try:
            yield
        finally:
            active["agent"] = None

    monkeypatch.setattr(turn, "_hermes_home_scope", home_scope)
    monkeypatch.setattr(turn, "_secret_scope", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(turn, "_terminal_scope", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    monkeypatch.setattr(turn, "_construct_agent", lambda *_a, **_k: _BrowserAgent())
    monkeypatch.setattr(turn.browser_control, "begin_turn", lambda *_a: object())

    def release(_lease):
        assert active["agent"] == "ceo", "browser release ran outside the agent profile scope"

    monkeypatch.setattr(turn.browser_control, "release", release)
    run()


def test_the_home_override_stays_active_through_run_conversation_and_is_reset_after(monkeypatch):
    state = {"home": None, "set_calls": 0, "reset_calls": 0}
    fake_hc = types.ModuleType("hermes_constants")

    def _set(path):
        state["home"] = path
        state["set_calls"] += 1
        return "TOKEN"

    def _reset(token):
        assert token == "TOKEN"
        state["home"] = None
        state["reset_calls"] += 1

    fake_hc.set_hermes_home_override = _set
    fake_hc.reset_hermes_home_override = _reset
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)

    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            # The override must still be active HERE, not only during construction.
            seen["home_during_run_conversation"] = state["home"]
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    turn._run_hermes("ceo", f.ingress(), None)

    assert seen["home_during_run_conversation"] == turn._profile_dir("ceo")
    assert state["set_calls"] == 1 and state["reset_calls"] == 1
    assert state["home"] is None  # reset after the turn, not leaked into the next one


def test_the_scope_is_a_no_op_when_hermes_constants_is_unimportable(monkeypatch):
    """This is what lets every other test in the suite replace _construct_agent without
    needing real Hermes internals -- confirms _run_hermes itself never raises for it."""
    monkeypatch.setattr(turn, "_construct_agent",
                        lambda *a, **k: type("A", (), {
                            "session_input_tokens": 0, "session_output_tokens": 0,
                            "session_cache_read_tokens": 0, "session_cache_write_tokens": 0,
                            "run_conversation": lambda self, **kw: {"api_calls": 1, "final_response": "ok"},
                        })())
    outcome, kind, reply, usage = turn._run_hermes("ceo", f.ingress(), None)
    assert (outcome, kind, reply) == ("completed", None, "ok")


def test_room_browser_release_runs_inside_the_agent_profile_scope(monkeypatch):
    _assert_browser_release_uses_profile_scope(
        monkeypatch, lambda: turn._run_hermes("ceo", f.ingress(), None)
    )


def test_native_browser_release_runs_inside_the_agent_profile_scope(monkeypatch):
    monkeypatch.setattr(turn, "paperclip_run_aborted", lambda _run_id: False)
    identity = {
        "agentId": "ceo", "runId": "run-1", "sessionId": "session-1",
        "paperclipApiUrl": "http://paperclip.internal", "companyId": "company-1",
    }
    _assert_browser_release_uses_profile_scope(
        monkeypatch,
        lambda: turn._run_native_paperclip(identity, {"input": "go", "instructions": "do"}),
    )


def test_turn_ensures_the_profile_before_pinning_defaults(monkeypatch):
    """Task 13b: ensure_profile() must run BEFORE pin_profile_defaults() -- a hot patch that
    landed since this process's last turn has to be pulled onto disk before the fill-missing
    pin reads (and re-writes) the profile's config.yaml, or the pin would fill gaps against a
    stale local copy and immediately re-upload it, clobbering the fresher backend write."""
    from bridge import sync

    calls = []
    monkeypatch.setattr(sync, "ensure_profile", lambda agent_id: calls.append(("ensure", agent_id)))
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id: calls.append(("pin", agent_id)))

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = lambda **kw: types.SimpleNamespace(
        run_conversation=lambda **kw: {"api_calls": 1, "final_response": "ok"},
    )
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(turn, "_session_db", lambda _a: object())

    turn._run_hermes("ceo", f.ingress(), None)
    assert calls == [("ensure", "ceo"), ("pin", "ceo")]
