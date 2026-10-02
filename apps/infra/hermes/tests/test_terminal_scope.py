"""The bridge binds Hermes's native terminal policy, without changing process globals."""
import os
import sys
from contextvars import ContextVar
from types import ModuleType, SimpleNamespace

import pytest

from bridge import sync, turn
from tests import factories as f


# ---- fix round: first turn after an image roll dies in image prep --------------------------
# pin_profile_defaults normalizes a legacy `terminal.backend: lightbulb_node` to `local`, but it
# used to run only INSIDE _construct_agent -- which _run_hermes calls AFTER _terminal_scope's
# `with` entry has already installed the scope. tools.terminal_scope.build_profile_terminal_scope
# reads config.yaml once, at THAT install instant, so a profile carrying the legacy value got a
# STALE `lightbulb_node` scope on the first turn after a roll: image_source._is_local_terminal_
# backend() read it back false, took the non-local fallback path, and _native_image_part raised
# _ImagePrepError. The second turn worked only because the first turn's pin had already
# rewritten the file. Fixed by normalizing before the `with` block, not inside construction.
def test_terminal_scope_installs_after_the_legacy_backend_is_already_normalized_on_disk(home, monkeypatch):
    sync.write_profile_config("ceo", {"terminal": {"backend": "lightbulb_node"}})

    seen = {}
    native = ModuleType("tools.terminal_scope")

    def install(profile, *, env_overlay):
        # The scope's own install reads config.yaml at THIS instant -- assert it already sees
        # the normalized value, not the legacy one still on disk when this test started.
        seen["backend_at_install"] = sync.read_profile_config("ceo").get("terminal", {}).get("backend")
        return "TOKEN"

    native.install_profile_terminal_scope = install
    native.reset_terminal_scope = lambda _t: None
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)

    class _Agent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _Agent())

    turn._run_hermes("ceo", f.ingress(), None)

    assert seen["backend_at_install"] == "local"


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
        assert active.get()["TERMINAL_CWD"].startswith(str(home / "profiles" / "ceo" / "work") + "/")
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

    with turn._terminal_scope("ceo", "agent:ceo:lightbulb:channel:r1"):
        pass

    dropped = sorted(k for k in seen if not k.startswith("TERMINAL_"))
    assert not dropped, f"only TERMINAL_* survives Hermes's overlay filter; {dropped} would be silently dropped"
    # TERMINAL_HOME_MODE is the CLI-connector lane's whole discovery mechanism: it puts $HOME at
    # <profile>/home, where the backend projects each CLI's own dotfiles (bridge/sync.py's
    # PROFILE_HOME). It is a policy key Hermes owns, which is exactly why it may ride here.
    assert set(seen) == {"TERMINAL_CWD", "TERMINAL_HOME_MODE"}


def test_a_native_turn_declares_its_child_env_names_without_leaking_any_value(home, monkeypatch):
    """The two halves the image then joins up: every name the profile can supply is registered
    with Hermes's per-session env passthrough (so its filter rescues it) and exists in the
    process env as an EMPTY placeholder (so the filter iterates it at all -- `_make_run_env`
    walks `dict(os.environ | env)`), while the VALUES stay in the per-profile secret scope."""
    profile = home / "profiles" / "agent-1"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("PAPERCLIP_API_KEY=pcp_live_x\nSOME_MCP_TOKEN=t\n")

    registered: set[str] = set()
    fake_ep = ModuleType("tools.env_passthrough")
    fake_ep.register_env_passthrough = registered.update
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", fake_ep)

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
    # Every name the scope can hold must start absent: `_declare_child_env_names` never
    # overwrites an existing process value (by design), and a GitHub-hosted runner sets
    # XDG_CONFIG_HOME, one of the CLI-connector names.
    for name in (
        "PAPERCLIP_API_KEY", "PAPERCLIP_RUN_ID", "PAPERCLIP_TASK_ID", "SOME_MCP_TOKEN",
        *sync.cli_credential_env("agent-1"),
    ):
        monkeypatch.delenv(name, raising=False)

    seen = {}

    class _Agent:
        model = "m"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            seen["passthrough"] = set(registered)
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

    # EXACTLY this turn's scope -- the per-turn identity names, the profile's own static .env
    # (including an owner-provisioned SOME_MCP_TOKEN the bridge cannot know ahead of time), and
    # the CLI-connector paths. No fixed contract: `register_env_passthrough` is per session, so
    # there is no cached list for a wrong-ordered turn to inherit.
    assert seen["passthrough"] == set(seen["scope"])
    assert {"PAPERCLIP_RUN_ID", "PAPERCLIP_TASK_ID", "SOME_MCP_TOKEN"} <= seen["passthrough"]
    assert seen["scope"]["PAPERCLIP_API_KEY"] == "pcp_live_x"
    assert seen["scope"]["PAPERCLIP_RUN_ID"] == "run-1"
    # Present (so Hermes's filter walks them) but VALUE-FREE: the value comes from the scope.
    # A contract name this turn holds no value for (the CLI lane's GOG_* on a profile with no
    # Google connected) is still listed and still declared -- it just resolves to nothing.
    assert set(seen["env"]) <= seen["passthrough"]
    assert set(seen["env"].values()) == {""}

    # Reviewer's finding on #2754 is now structural rather than a list to keep in step: a lane
    # that adds a per-turn overlay name gets it registered automatically, because the registered
    # set IS the scope. The equality assertion above is what keeps that true.
    static_env_names = {
        line.split("=", 1)[0] for line in (profile / ".env").read_text().splitlines() if "=" in line
    }
    assert set(seen["scope"]) - static_env_names, "the per-turn overlay should not be empty here"


def test_the_names_are_registered_through_hermes_own_per_session_api(home, monkeypatch):
    """The whole reason the bridge used to carry a fixed name contract was the OTHER door:
    `terminal.env_passthrough` in config.yaml is cached per Hermes home for the PROCESS
    LIFETIME with no invalidation, so whichever turn ran a shell command first froze the list,
    and a room turn's scope holds only the static `.env`.

    `register_env_passthrough` is ContextVar-scoped per session and is consulted FIRST by
    `is_env_passthrough`, so there is no list to freeze. This pins that the bridge uses that
    API and hands it exactly the turn's own scope; the end-to-end proof is
    `tests/smoke/paperclip_child_env_smoke.py`, which needs the image.
    """
    registered: list[list[str]] = []
    native = ModuleType("tools.env_passthrough")
    native.register_env_passthrough = lambda names: registered.append(sorted(names))
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", native)

    turn._allow_child_env_names({"PAPERCLIP_RUN_ID": "run-1", "SOME_MCP_TOKEN": "t"})
    assert registered == [["PAPERCLIP_RUN_ID", "SOME_MCP_TOKEN"]]

    # And the cached config.yaml door is never written back: pin_profile_defaults no longer
    # touches terminal.env_passthrough at all.
    (home / "profiles" / "ceo").mkdir(parents=True)
    turn.sync.write_profile_config("ceo", {})
    monkeypatch.setattr(turn.sync, "_aux_defaults", lambda: {})
    config = turn.sync.pin_profile_defaults("ceo")
    assert "env_passthrough" not in (config.get("terminal") or {})


def _fake_session_context(monkeypatch):
    """Hermes keys a terminal environment (and its re-sourced `export -p` snapshot) by the bound
    HERMES_SESSION_KEY; unbound, the whole process shares one "default" shell -- $HOME froze at
    the first profile and one agent's `export` leaked into every other agent's commands. The
    real-shell proof is tests/smoke/session_env_isolation_smoke.py (needs the image)."""
    bound = ContextVar("key", default=None)
    seen = []
    native = ModuleType("tools.terminal_scope")
    native.install_profile_terminal_scope = lambda _p, *, env_overlay: "T"
    native.reset_terminal_scope = lambda _t: None
    ctx = ModuleType("gateway.session_context")

    def set_session_vars(**kw):
        assert set(kw) == {"session_key"}, "bind only the key; the rest changes routing semantics"
        seen.append(kw["session_key"])
        return bound.set(kw["session_key"])

    ctx.set_session_vars = set_session_vars
    ctx.clear_session_vars = lambda _tokens: bound.set("")
    monkeypatch.setitem(sys.modules, "tools.terminal_scope", native)
    monkeypatch.setitem(sys.modules, "gateway", ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)
    return bound, seen


def test_each_agent_and_session_gets_its_own_terminal_key(monkeypatch):
    bound, seen = _fake_session_context(monkeypatch)
    cases = {
        ("ceo", "agent:ceo:lightbulb:channel:r1"): "agent:ceo:lightbulb:channel:r1",
        ("engineering", "agent:engineering:lightbulb:channel:r1"): "agent:engineering:lightbulb:channel:r1",
        ("ceo", "agent:ceo:lightbulb:channel:r2"): "agent:ceo:lightbulb:channel:r2",
        ("a1", "paperclip:company:c1:agent:a1:issue:i1"): "paperclip:company:c1:agent:a1:issue:i1",
        # A mail key names no agent: two agents on one thread must not share a shell.
        ("ceo", "lightbulb-email:abc"): "agent:ceo:lightbulb-email:abc",
        ("engineering", "lightbulb-email:abc"): "agent:engineering:lightbulb-email:abc",
    }
    for (agent, key), want in cases.items():
        with turn._terminal_scope(agent, key):
            assert bound.get() == want
        assert bound.get() == "", "the binding must not outlive the turn"
    assert len(set(seen)) == len(cases)


def test_a_room_turn_binds_its_own_session_key_for_the_whole_run(home, monkeypatch):
    bound, _seen = _fake_session_context(monkeypatch)
    during = []

    class _Agent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            during.append(bound.get())
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _Agent())
    body = f.ingress()
    turn._run_hermes("ceo", body, None)
    assert during == [turn._resolve_session_key("ceo", body)]
