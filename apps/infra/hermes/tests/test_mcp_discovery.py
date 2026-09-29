"""P0: the bridge builds AIAgent itself, so it must run Hermes's MCP discovery -- upstream removed
it from model_tools import time (model_tools.py:152-160 on v2026.9.14) and each entry point runs
it itself. It runs per profile, inside that profile's home + secret scope, the way the gateway's
multiplexed ``_discover_gateway_mcp_tools`` does (gateway/run.py:1795-1820): Hermes keys the
MCP registry by ``hermes_home_key()`` under multiplex, and resolves the token dir and ``${VAR}``s
from the scope active at discovery time."""
import sys
import types
from contextlib import contextmanager, nullcontext

import pytest

from bridge import turn
from tests import factories as f


@pytest.fixture
def mcp(monkeypatch):
    state = {"home": None, "secrets": None, "config": {}, "registered": {}, "calls": [],
             "shutdowns": [], "fail": set(), "seen": []}

    @contextmanager
    def scoped(key, agent_id, *_a, **_k):
        state[key] = agent_id
        try:
            yield
        finally:
            state[key] = None

    monkeypatch.setattr(turn, "_hermes_home_scope", lambda a: scoped("home", a))
    monkeypatch.setattr(turn, "_secret_scope", lambda a, *x, **k: scoped("secrets", a))
    monkeypatch.setattr(turn, "_terminal_scope", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(turn, "runtime_mcp_env", lambda _a: {})
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    monkeypatch.setattr(turn.browser_control, "begin_turn", lambda *_a: None)
    monkeypatch.setattr(turn.browser_control, "release", lambda _l: None)
    monkeypatch.setattr(turn, "_MCP_FINGERPRINTS", {})

    def load():
        return dict(state["config"].get(state["home"], {}))

    def discover():
        state["calls"].append((state["home"], state["secrets"]))
        if state["home"] in state["fail"]:
            raise TimeoutError("MCP call timed out after 120.0s")
        state["registered"][state["home"]] = [f"mcp_{n}_t" for n in load()]
        return state["registered"][state["home"]]

    def status(configured=None):
        return [{"name": n, "tools": 1, "status": "connected"} for n in (configured or load())]

    def shutdown(*, scope=None, names=None):
        state["shutdowns"].append(scope)
        state["registered"].pop(scope, None)

    fakes = {
        "tools": {},
        "tools.mcp_oauth": {"suppress_interactive_oauth": nullcontext},
        "tools.mcp_tool_config": {"_load_mcp_config": load},
        "tools.mcp_tool_discovery": {"discover_mcp_tools": discover, "get_mcp_status": status},
        "tools.mcp_tool_lifecycle": {"shutdown_mcp_servers": shutdown},
        "tools.registry": {"registry": types.SimpleNamespace(current_scope_key=lambda: state["home"])},
    }
    for name, attrs in fakes.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)

    class _Agent:
        model = "m"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            return {"api_calls": 1, "final_response": "ok"}

    def construct(agent_id, *_a, **_k):
        state["seen"].append((agent_id, list(state["registered"].get(state["home"], []))))
        return _Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)
    return state


def test_each_profile_turn_sees_only_its_own_mcp_servers(mcp):
    mcp["config"] = {"ceo": {"quickbooks": {"command": "q"}}, "eng": {"stripe": {"url": "u"}}}
    for agent in ("ceo", "eng", "ceo"):
        assert turn._run_hermes(agent, f.ingress(), None)[0] == "completed"
    assert mcp["seen"] == [("ceo", ["mcp_quickbooks_t"]), ("eng", ["mcp_stripe_t"]),
                           ("ceo", ["mcp_quickbooks_t"])]
    # Discovery ran inside the CURRENT agent's home (token dir) AND secret scope (${VAR}s).
    assert mcp["calls"] == [("ceo", "ceo"), ("eng", "eng"), ("ceo", "ceo")]


def test_native_paperclip_turn_discovers_in_its_profile(mcp):
    mcp["config"] = {"eng": {"linear": {"url": "u"}}}
    identity = {"agentId": "eng", "runId": "r1", "sessionId": "s1", "paperclipApiUrl": "p",
                "companyId": "c"}
    turn._run_native_paperclip(identity, {"input": "hi", "instructions": "x"})
    assert mcp["seen"] == [("eng", ["mcp_linear_t"])]
    assert mcp["calls"] == [("eng", "eng")]


def test_native_paperclip_turn_brokers_remote_mcp_access(mcp, monkeypatch):
    seen = {}
    monkeypatch.setattr(turn, "runtime_mcp_env", lambda _agent: {"MCP_POSTHOG_ACCESS_TOKEN": "access-token"})

    @contextmanager
    def secret_scope(agent_id, *_args):
        seen["overlay"] = turn._brokered_mcp_overlay.get()
        mcp["secrets"] = agent_id
        try:
            yield
        finally:
            mcp["secrets"] = None

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)
    mcp["config"] = {"eng": {"posthog": {"url": "https://mcp.posthog.com/mcp?mode=cli"}}}
    identity = {"agentId": "eng", "runId": "r1", "sessionId": "s1", "paperclipApiUrl": "p", "companyId": "c"}

    assert turn._run_native_paperclip(identity, {"input": "hi", "instructions": "x"})[0] == "completed"
    assert seen["overlay"] == {"MCP_POSTHOG_ACCESS_TOKEN": "access-token"}
    assert mcp["calls"] == [("eng", "eng")]


def test_a_failing_discovery_does_not_fail_the_turn(mcp):
    mcp["config"] = {"ceo": {"quickbooks": {"command": "q"}}}
    mcp["fail"] = {"ceo"}
    assert turn._run_hermes("ceo", f.ingress(), None)[0] == "completed"
    assert mcp["seen"] == [("ceo", [])]


def test_changed_mcp_config_reconnects_only_that_profile(mcp):
    mcp["config"] = {"ceo": {"quickbooks": {"command": "q"}}, "eng": {"stripe": {"url": "u"}}}
    turn._run_hermes("ceo", f.ingress(), None)
    turn._run_hermes("eng", f.ingress(), None)
    turn._run_hermes("ceo", f.ingress(), None)
    assert mcp["shutdowns"] == []  # unchanged config: keep the live connections
    # A .revision re-pull rewrote ceo's config.yaml (e.g. a newly connected Notion).
    mcp["config"]["ceo"] = {"quickbooks": {"command": "q"}, "notion": {"url": "n"}}
    turn._run_hermes("ceo", f.ingress(), None)
    assert mcp["shutdowns"] == ["ceo"]
    assert mcp["seen"][-1] == ("ceo", ["mcp_quickbooks_t", "mcp_notion_t"])
    assert mcp["registered"]["eng"] == ["mcp_stripe_t"]
