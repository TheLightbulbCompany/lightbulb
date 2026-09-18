"""The bridge binds Hermes's native terminal policy, without changing process globals."""
import os
import sys
from contextvars import ContextVar
from types import ModuleType, SimpleNamespace

import pytest

from bridge import turn
from tests import factories as f


@pytest.mark.parametrize("fails", [False, True])
def test_profile_terminal_scope_covers_construction_and_run_and_resets(home, monkeypatch, fails):
    active = ContextVar("terminal", default=None)
    native = ModuleType("tools.terminal_scope")
    refreshed = []

    def install(profile, *, env_overlay):
        assert refreshed == ["ceo"]
        assert profile == home / "profiles" / "ceo"
        return active.set(env_overlay)

    native.install_profile_terminal_scope = install
    native.reset_terminal_scope = active.reset
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)
    monkeypatch.setattr(turn.sync, "ensure_profile", refreshed.append)
    monkeypatch.setenv("TERMINAL_CWD", "/not-this-profile")
    process_cwd = os.getcwd()

    def check_scope():
        assert active.get() == {"TERMINAL_CWD": str(home / "profiles" / "ceo")}
        assert os.environ["TERMINAL_CWD"] == "/not-this-profile"
        assert os.getcwd() == process_cwd

    def run(**kwargs):
        check_scope()
        if fails:
            raise RuntimeError("turn failed")
        return {"api_calls": 1, "final_response": "done"}

    def construct(*args):
        check_scope()
        return SimpleNamespace(run_conversation=run)

    monkeypatch.setattr(turn, "_construct_agent", construct)
    if fails:
        with pytest.raises(RuntimeError, match="turn failed"):
            turn._run_hermes("ceo", f.ingress(), None)
    else:
        assert turn._run_hermes("ceo", f.ingress(), None)[0] == "completed"
    assert active.get() is None


def test_a_native_paperclip_turn_hands_the_terminal_child_its_key_and_run_identity(home, monkeypatch):
    """The whole point of the native lane is that the agent can disposition the issue, which it
    does by calling the Paperclip API from a terminal command. A terminal child inherits NEITHER
    the profile ``.env`` NOR the secret scope (measured in the pinned image:
    ``PROFILE_CREDENTIAL=False``), so the key and the run identity must ride Hermes's own
    ``env_overlay`` -- and nothing may reach ``os.environ``, which two concurrent profiles in one
    process would race over."""
    profile = home / "profiles" / "agent-1"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\nUNRELATED=nope\n")

    active = ContextVar("terminal", default=None)
    native = ModuleType("tools.terminal_scope")
    native.install_profile_terminal_scope = lambda _profile, *, env_overlay: active.set(env_overlay)
    native.reset_terminal_scope = active.reset
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)

    fake_ss = ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = lambda home_dir: dict(
        line.split("=", 1) for line in (home_dir / ".env").read_text().splitlines() if "=" in line
    )
    fake_ss.set_secret_scope = lambda mapping: None
    fake_ss.reset_secret_scope = lambda token: None
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)

    seen = {}

    class _Agent:
        model = "m"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            seen["overlay"] = dict(active.get() or {})
            return {"api_calls": 1, "final_response": "done"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: _Agent())
    before = dict(os.environ)

    identity = {
        "runId": "run-1",
        "companyId": "company-1",
        "agentId": "agent-1",
        "sessionId": "paperclip:company:company-1:agent:agent-1:issue:issue-1",
        "issueId": "issue-1",
        "paperclipApiUrl": "http://paperclip.internal:3100",
    }
    request = {"input": "go", "instructions": "do"}
    turn.register_paperclip_run(identity, request)
    assert turn._run_native_paperclip(identity, request)[0] == "completed"

    assert seen["overlay"] == {
        "TERMINAL_CWD": str(profile),
        "PAPERCLIP_API_KEY": "pcp_live_x",  # the .env key, never exported process-wide
        "PAPERCLIP_API_URL": "http://paperclip.internal:3100",
        "PAPERCLIP_AGENT_ID": "agent-1",
        "PAPERCLIP_COMPANY_ID": "company-1",
        "PAPERCLIP_RUN_ID": "run-1",
        "PAPERCLIP_TASK_ID": "issue-1",
    }
    assert active.get() is None  # reset after the turn, never left installed
    assert dict(os.environ) == before
