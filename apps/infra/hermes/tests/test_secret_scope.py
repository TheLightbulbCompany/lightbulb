"""Task 13b, the load-bearing item (verified in the lightbulb/hermes-spike:m1-rc4 image): a
profile's ``.env`` VALUES are not loaded automatically anywhere in this process. ``run_agent.py``
loads exactly ONE home's ``.env`` into ``os.environ`` at import (the process's own root/launch
profile — grep confirms `_hermes_home = get_hermes_home()` then `load_hermes_dotenv(hermes_home=
_hermes_home, ...)` at module level), and Hermes's own multiplex guard
(``hermes_cli/env_loader.py``, "multiplex: skipping process-global dotenv load for routed profile
home") deliberately skips the process-global dotenv once multiplexing + a home override are
active. So a ``${VAR}`` reference in a SECONDARY profile's ``config.yaml`` (``_env_ref_lookup``,
``hermes_cli/config.py``) resolves via ``agent.secret_scope.get_secret`` — which raises fail-closed
under multiplexing with no scope installed, or (via ``_env_ref_lookup``'s own no-scope-at-all
short-circuit) silently falls back to ``os.environ`` — never automatically that profile's own
``.env``. This scope is the ONLY thing that makes it resolve correctly.
"""

import contextvars
import os
import sys
import types
from pathlib import Path

from bridge import sync, turn


def test_the_secret_scope_stays_active_through_run_conversation_and_is_reset_after(monkeypatch):
    state = {"scope": None, "home_seen": None, "set_calls": 0, "reset_calls": 0}
    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")

    def _build(home):
        state["home_seen"] = home
        return {"PAPERCLIP_API_KEY": "pcp_x"}

    def _set(secrets):
        state["scope"] = secrets
        state["set_calls"] += 1
        return "TOKEN"

    def _reset(token):
        assert token == "TOKEN"
        state["scope"] = None
        state["reset_calls"] += 1

    fake_ss.build_profile_secret_scope = _build
    fake_ss.set_secret_scope = _set
    fake_ss.reset_secret_scope = _reset
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            # The scope must still be installed HERE, not only during construction.
            seen["scope_during_run_conversation"] = dict(state["scope"] or {})
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    from tests import factories as f

    turn._run_hermes("ceo", f.ingress(), None)

    assert seen["scope_during_run_conversation"] ["PAPERCLIP_API_KEY"] == "pcp_x"
    assert state["home_seen"] == sync._profile_dir("ceo")
    assert state["set_calls"] == 1 and state["reset_calls"] == 1
    assert state["scope"] is None  # reset after the turn, not leaked into the next one


def test_the_secret_scope_is_installed_before_construct_agent_runs(monkeypatch):
    """Minor 10 (fix round 1 continuation): the scope must wrap CONSTRUCTION too, not only
    run_conversation -- report 01 §3's own words are "config is read once per AIAgent
    construction", and a ${VAR} ref resolved at that moment needs the scope already installed."""
    state = {"scope": None}
    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = lambda home: {"PAPERCLIP_API_KEY": "pcp_x"}
    fake_ss.set_secret_scope = lambda secrets: state.update(scope=secrets) or "TOKEN"
    fake_ss.reset_secret_scope = lambda token: state.update(scope=None)
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    seen = {}

    def fake_construct_agent(agent_id, body, frames, session_key):
        # The scope must already be installed HERE -- at construction time, before any turn runs.
        seen["scope_during_construction"] = dict(state["scope"] or {})

        class _FakeAgent:
            session_input_tokens = session_output_tokens = 0
            session_cache_read_tokens = session_cache_write_tokens = 0

            def run_conversation(self, **_kw):
                return {"api_calls": 1, "final_response": "ok"}

        return _FakeAgent()

    monkeypatch.setattr(turn, "_construct_agent", fake_construct_agent)
    from tests import factories as f

    turn._run_hermes("ceo", f.ingress(), None)

    assert seen["scope_during_construction"] ["PAPERCLIP_API_KEY"] == "pcp_x"


def test_the_secret_scope_is_a_no_op_when_agent_secret_scope_is_unimportable(monkeypatch):
    """Same convention as _hermes_home_scope: dev/test environments outside the Hermes image
    have no agent.secret_scope module, and every other test in this suite (that doesn't stub it
    in) relies on this being a silent no-op rather than an ImportError."""
    monkeypatch.setattr(
        turn,
        "_construct_agent",
        lambda *a, **k: type(
            "A",
            (),
            {
                "session_input_tokens": 0,
                "session_output_tokens": 0,
                "session_cache_read_tokens": 0,
                "session_cache_write_tokens": 0,
                "run_conversation": lambda self, **kw: {"api_calls": 1, "final_response": "ok"},
            },
        )(),
    )
    from tests import factories as f

    outcome, kind, reply, _usage = turn._run_hermes("ceo", f.ingress(), None)
    assert (outcome, kind, reply) == ("completed", None, "ok")


# ---- the load-bearing resolution check ----------------------------------------------------
# A minimal but functionally real stand-in for agent.secret_scope's contextvar mechanism (not a
# call recorder) — real enough that a value only becomes readable inside the `with` block and is
# genuinely gone after, the same guarantee the real module's ContextVar.set/.reset provides.
_TEST_SCOPE: contextvars.ContextVar = contextvars.ContextVar("_TEST_SCOPE", default=None)


def _stub_set_secret_scope(mapping):
    return _TEST_SCOPE.set(mapping)


def _stub_reset_secret_scope(token):
    _TEST_SCOPE.reset(token)


def _stub_get_secret(name):
    return (_TEST_SCOPE.get() or {}).get(name)


def _stub_build_profile_secret_scope(home):
    """A minimal real ``.env`` reader — proving the WIRING (right path in, right values out),
    not the full grammar (that is I3's job in the backend lane's render_dotenv)."""
    env_path = Path(home) / ".env"
    out = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_a_paperclip_api_key_reference_resolves_from_the_profiles_env_under_the_scope(home, monkeypatch):
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\n")

    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = _stub_build_profile_secret_scope
    fake_ss.set_secret_scope = _stub_set_secret_scope
    fake_ss.reset_secret_scope = _stub_reset_secret_scope
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    assert _stub_get_secret("PAPERCLIP_API_KEY") is None  # nothing installed yet
    with turn._secret_scope("ceo"):
        assert _stub_get_secret("PAPERCLIP_API_KEY") == "pcp_live_x"
    assert _stub_get_secret("PAPERCLIP_API_KEY") is None  # reset after


def test_managed_gate_bearer_comes_from_boot_secret_not_profile_dotenv(home, monkeypatch):
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\nLIGHTBULB_GATE_BEARER=attacker\n")
    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = _stub_build_profile_secret_scope
    fake_ss.set_secret_scope = _stub_set_secret_scope
    fake_ss.reset_secret_scope = _stub_reset_secret_scope
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "boot-gate-bearer")

    with turn._secret_scope("ceo"):
        assert _stub_get_secret("LIGHTBULB_GATE_BEARER") == "boot-gate-bearer"
    assert _stub_get_secret("LIGHTBULB_GATE_BEARER") is None


def test_agents_own_gate_bearer_wins_over_boot_one_and_never_reaches_a_child(home, monkeypatch):
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\nLIGHTBULB_AGENT_GATE_BEARER=agent-bearer\n")
    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = _stub_build_profile_secret_scope
    fake_ss.set_secret_scope = _stub_set_secret_scope
    fake_ss.reset_secret_scope = _stub_reset_secret_scope
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "boot-gate-bearer")
    declared = []
    monkeypatch.setattr(turn, "_declare_child_env_names", lambda names: declared.extend(names))

    with turn._secret_scope("ceo"):
        assert _stub_get_secret("LIGHTBULB_GATE_BEARER") == "agent-bearer"
    assert "LIGHTBULB_AGENT_GATE_BEARER" not in declared and "LIGHTBULB_GATE_BEARER" not in declared


def test_native_paperclip_identity_overlays_profile_secrets_without_process_leak(home, monkeypatch):
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\n")

    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = _stub_build_profile_secret_scope
    fake_ss.set_secret_scope = _stub_set_secret_scope
    fake_ss.reset_secret_scope = _stub_reset_secret_scope
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    dynamic = {
        "PAPERCLIP_API_URL": "http://paperclip.internal:3100",
        "PAPERCLIP_AGENT_ID": "ceo",
        "PAPERCLIP_COMPANY_ID": "company-1",
        "PAPERCLIP_RUN_ID": "run-1",
        "PAPERCLIP_TASK_ID": "issue-1",
    }
    for name in {"PAPERCLIP_API_KEY", *dynamic}:
        monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    with turn._secret_scope("ceo", dynamic):
        assert _stub_get_secret("PAPERCLIP_API_KEY") == "pcp_live_x"
        assert {key: _stub_get_secret(key) for key in dynamic} == dynamic
    assert _TEST_SCOPE.get() is None
    # The scope adds each NAME to the process env as an empty placeholder -- that is the only
    # thing that makes Hermes's child-env filter iterate it (`_make_run_env` walks
    # `dict(os.environ | env)`), and a name carries no tenant data. What must never appear
    # process-wide is a VALUE: those stay in the per-profile scope, where
    # `resolve_passthrough_value` reads them, so two profiles cannot see each other's.
    added = {k: v for k, v in os.environ.items() if k not in before}
    assert set(added) == {"PAPERCLIP_API_KEY", *dynamic}
    assert set(added.values()) == {""}
    assert {k: v for k, v in os.environ.items() if k in before} == before
