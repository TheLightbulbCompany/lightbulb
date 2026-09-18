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


def test_env_overlay_carries_only_terminal_policy_not_arbitrary_child_env(home, monkeypatch):
    """A REGRESSION FENCE, not a shape assertion.

    `build_profile_terminal_scope` keeps only keys prefixed `TERMINAL_`
    (`tools/terminal_scope.py:137` in `lightbulb/hermes-spike:m1-rc4`). An earlier pass on
    this branch used `env_overlay` to deliver `PAPERCLIP_*` to a shell; it passed a mocked
    test and delivered nothing. So this asserts the bridge does NOT put anything else there
    -- the real child-env contract is `terminal.env_passthrough` + the secret scope, and its
    end-to-end proof is `tests/smoke/paperclip_child_env_smoke.py`, which needs the image."""
    seen = {}
    native = ModuleType("tools.terminal_scope")
    native.install_profile_terminal_scope = lambda _p, *, env_overlay: seen.update(env_overlay) or "T"
    native.reset_terminal_scope = lambda _t: None
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)

    with turn._terminal_scope("ceo"):
        pass

    dropped = sorted(k for k in seen if not k.startswith("TERMINAL_"))
    assert not dropped, f"only TERMINAL_* survives Hermes's overlay filter; {dropped} would be silently dropped"
    # TERMINAL_HOME_MODE is the CLI-connector lane's whole discovery mechanism: it puts $HOME at
    # <profile>/home, where the backend projects each CLI's own dotfiles (bridge/sync.py's
    # PROFILE_HOME). It is a policy key Hermes owns, which is exactly why it may ride here.
    assert set(seen) == {"TERMINAL_CWD", "TERMINAL_HOME_MODE"}


def test_a_native_turn_declares_its_child_env_names_without_leaking_any_value(home, monkeypatch):
    """The two halves the image then joins up: every name the profile can supply is listed in
    `terminal.env_passthrough` (so Hermes's filter rescues it) and exists in the process env
    as an EMPTY placeholder (so the filter iterates it at all -- `_make_run_env` walks
    `dict(os.environ | env)`), while the VALUES stay in the per-profile secret scope."""
    profile = home / "profiles" / "agent-1"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\nSOME_MCP_TOKEN=t\n")

    installed = {}
    fake_ss = ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = lambda home_dir: dict(
        line.split("=", 1) for line in (home_dir / ".env").read_text().splitlines() if "=" in line
    )
    fake_ss.set_secret_scope = lambda mapping: installed.update(mapping) or "T"
    fake_ss.reset_secret_scope = lambda _t: installed.clear()
    fake_ss.current_secret_scope = lambda: dict(installed)
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    for name in ("PAPERCLIP_API_KEY", "PAPERCLIP_RUN_ID", "PAPERCLIP_TASK_ID", "SOME_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    seen = {}

    class _Agent:
        model = "m"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            seen["passthrough"] = set(turn.sync.profile_child_env_names())
            seen["scope"] = dict(installed)
            seen["env"] = {k: os.environ.get(k) for k in seen["scope"]}
            return {"api_calls": 1, "final_response": "done"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: _Agent())

    identity = {
        "runId": "run-1", "companyId": "company-1", "agentId": "agent-1", "issueId": "issue-1",
        "sessionId": "paperclip:company:company-1:agent:agent-1:issue:issue-1",
        "paperclipApiUrl": "http://paperclip.internal:3100",
    }
    request = {"input": "go", "instructions": "do"}
    turn.register_paperclip_run(identity, request)
    assert turn._run_native_paperclip(identity, request)[0] == "completed"

    # The bridge's fixed contract UNION the live scope, so an owner-provisioned name the bridge
    # cannot know ahead of time (SOME_MCP_TOKEN) is carried too.
    assert seen["passthrough"] == {*turn.sync.BRIDGE_CHILD_ENV_NAMES, "SOME_MCP_TOKEN"}
    assert seen["scope"]["PAPERCLIP_API_KEY"] == "pcp_live_x"
    assert seen["scope"]["PAPERCLIP_RUN_ID"] == "run-1"
    # Present (so Hermes's filter walks them) but VALUE-FREE: the value comes from the scope.
    # A contract name this turn holds no value for (the CLI lane's GOG_* on a profile with no
    # Google connected) is still listed and still declared -- it just resolves to nothing.
    assert set(seen["env"]) <= seen["passthrough"]
    assert set(seen["env"].values()) == {""}

    # Reviewer's finding on #2754, cheapest to pin here: every name the bridge puts in the
    # per-turn OVERLAY (the scope minus the profile's own static .env) must also be in the fixed
    # contract. Without this, a lane that adds an overlay name and forgets the tuple still
    # passes every test -- and then fails only under room-turn-first ordering in a warm microVM,
    # where Hermes has already cached a name list that does not contain it.
    static_env_names = {
        line.split("=", 1)[0] for line in (profile / ".env").read_text().splitlines() if "=" in line
    }
    overlay_names = set(seen["scope"]) - static_env_names
    assert overlay_names <= set(turn.sync.BRIDGE_CHILD_ENV_NAMES), (
        f"{sorted(overlay_names - set(turn.sync.BRIDGE_CHILD_ENV_NAMES))} reach the scope per-turn but are "
        "missing from BRIDGE_CHILD_ENV_NAMES -- they would be dropped whenever a room turn runs first"
    )


def test_the_declared_name_list_does_not_depend_on_what_this_turn_happens_to_hold(home, monkeypatch):
    """Hermes caches ``terminal.env_passthrough`` per home for the PROCESS LIFETIME with no
    invalidation, and one warm microVM serves room turns AND Paperclip runs. So whichever turn
    runs a shell command first decides the list for every later turn — and a room turn's scope
    holds only the static `.env`. Deriving the list from the live scope froze it without the
    five per-turn identity names, and the native turn then got them as EMPTY strings (caught
    in-image; `tests/smoke/paperclip_child_env_smoke.py` runs a room turn first for this reason).

    So the NAME list is a fixed contract, stable no matter what a given turn holds. A unit test
    cannot prove Hermes's caching; it can prove the list no longer varies, which is the
    property the cache needs."""
    fake_ss = ModuleType("agent.secret_scope")
    fake_ss.current_secret_scope = lambda: {"PAPERCLIP_API_KEY": "k"}  # a ROOM turn's scope
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)
    room_turn_names = set(turn.sync.profile_child_env_names())

    fake_ss.current_secret_scope = lambda: None  # no scope installed at all
    unscoped_names = set(turn.sync.profile_child_env_names())

    contract = set(turn.sync.BRIDGE_CHILD_ENV_NAMES)
    assert contract <= room_turn_names and contract <= unscoped_names
    assert {"PAPERCLIP_API_URL", "PAPERCLIP_RUN_ID", "PAPERCLIP_TASK_ID"} <= room_turn_names
