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
        # The overlay also carries the CLI-connector config-location vars (test_cli_credentials);
        # what this test guards is that it is an OVERLAY, never a process global.
        assert active.get()["TERMINAL_CWD"] == str(home / "profiles" / "ceo")
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
