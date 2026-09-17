"""`lightbulb_node`: the terminal environment provider that runs a Hermes turn's shell
commands on the owner's paired desktop through the backend's node route.

Most of this module is plain and needs no Hermes image on the path (M2-R7: it never imports
`agent.terminal_env_provider` at module scope). Only `register()`/`_provider()` -- the "provider
factory" -- does that import, lazily, so those two tests stub `sys.modules["agent.terminal_env_provider"]`
with a minimal base class the way test_home_scope.py stubs `hermes_constants`; the real class was
read from the image (see the lane report for the exact `docker run` commands and names).
"""
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb_node  # noqa: E402


def env(monkeypatch, result):
    monkeypatch.setattr(lightbulb_node, "_invoke", lambda command, params, timeout_ms: result)
    return lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/Users/x", timeout=180)


def test_the_provider_is_remote_but_not_a_container_so_approvals_stay_on():
    provider = lightbulb_node.LightbulbNodeProvider()
    assert provider.name == "lightbulb_node"
    assert provider.is_remote is True and provider.is_container is False
    assert provider.strip_env_keys == frozenset({"AWS_BEARER_TOKEN_BEDROCK", "PAPERCLIP_API_KEY"})


def test_a_command_is_wrapped_in_bash_lc_and_sent_as_params_json(monkeypatch):
    seen = {}
    monkeypatch.setattr(lightbulb_node, "_invoke",
                        lambda command, params, timeout_ms: seen.update(
                            command=command, params=params, timeout_ms=timeout_ms) or
                        {"ok": True, "payload": {"stdout": "ok\n", "stderr": "", "exitCode": 0,
                                                 "timedOut": False, "success": True}})
    out = lightbulb_node.LightbulbNodeProvider().create_environment(
        cwd="/Users/x", timeout=180).execute("echo ok", timeout=30)
    assert seen["command"] == "system.run"
    assert seen["params"]["command"] == ["bash", "-lc", "echo ok"]
    assert seen["params"]["cwd"] == "/Users/x" and seen["params"]["timeoutMs"] == 30_000
    assert out == {"output": "ok\n", "returncode": 0}


def test_stdout_and_stderr_are_concatenated_and_a_nonzero_exit_is_a_normal_result(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"stdout": "a", "stderr": "b", "exitCode": 2,
                                                    "timedOut": False, "success": False}}).execute("x")
    assert out == {"output": "ab", "returncode": 2}


def test_a_timeout_uses_hermes_sentinel_124_with_the_expected_suffix(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"stdout": "partial", "stderr": "",
                                                    "timedOut": True}}).execute("sleep 99", timeout=5)
    assert out["returncode"] == 124 and out["output"].endswith("[Command timed out after 5s]")


def test_a_spawn_failure_and_a_transport_failure_both_return_returncode_none(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"error": "spawn ENOENT", "stdout": "", "stderr": ""}}).execute("x")
    assert out["returncode"] is None and "spawn ENOENT" in out["output"]
    out = env(monkeypatch, {"ok": False, "error": {"code": "UNAVAILABLE", "message": "no node"}}).execute("x")
    assert out["returncode"] is None and "UNAVAILABLE" in out["output"]


def test_cleanup_never_touches_the_users_mac(monkeypatch):
    assert env(monkeypatch, {"ok": True, "payload": {}}).cleanup() is None


def test_is_available_requires_both_the_api_url_and_the_hook_token(monkeypatch):
    monkeypatch.delenv("ISOL8_API_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_HOOK_TOKEN", raising=False)
    provider = lightbulb_node.LightbulbNodeProvider()
    assert provider.is_available() is False
    monkeypatch.setenv("ISOL8_API_URL", "https://api-dev.flickmeon.ai")
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "tok")
    assert provider.is_available() is True


def test_invoke_reads_the_hook_token_from_the_process_env_not_a_turn_context_file(monkeypatch):
    """M2-R12: exec is not turn-scoped, so the bearer must come from the process env the boot
    fetch (bridge/__init__.py) writes into -- never a per-turn `.lightbulb-turn` file."""
    monkeypatch.setenv("ISOL8_API_URL", "https://api-dev.flickmeon.ai")
    monkeypatch.setenv("ISOL8_OWNER_ID", "org_1")
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "env-hook-token")
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"ok": True, "payload": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    lightbulb_node._invoke("system.run", {"command": ["bash", "-lc", "x"]}, 30_000)
    assert captured["auth"] == "Bearer env-hook-token"
    assert captured["url"] == "https://api-dev.flickmeon.ai/api/v1/internal/node/invoke/org_1"


def _stub_terminal_env_provider(monkeypatch):
    """M2-R7: minimal real base, matching agent/terminal_env_provider.py's actual abstract
    surface (name/is_available/create_environment), stubbed the way test_home_scope.py stubs
    hermes_constants -- this test runner has no Hermes image on the path."""
    import types

    fake_pkg = types.ModuleType("agent")
    fake_mod = types.ModuleType("agent.terminal_env_provider")

    class TerminalEnvironmentProvider:
        is_remote = True
        is_container = True
        session_isolated_when_nonpersistent = False

        def is_available(self):
            raise NotImplementedError

        def create_environment(self, *, cwd, timeout, task_id="default", image=None,
                               container_config=None, **kwargs):
            raise NotImplementedError

    fake_mod.TerminalEnvironmentProvider = TerminalEnvironmentProvider
    monkeypatch.setitem(sys.modules, "agent", fake_pkg)
    monkeypatch.setitem(sys.modules, "agent.terminal_env_provider", fake_mod)
    return fake_mod.TerminalEnvironmentProvider


def test_provider_is_built_lazily_and_really_isa_the_real_terminal_environment_provider(monkeypatch):
    real_base = _stub_terminal_env_provider(monkeypatch)
    provider = lightbulb_node._provider()
    assert isinstance(provider, real_base)
    assert isinstance(provider, lightbulb_node.LightbulbNodeProvider)
    assert provider.name == "lightbulb_node"


def test_register_wires_the_provider_into_the_terminal_environment_registry(monkeypatch):
    _stub_terminal_env_provider(monkeypatch)
    seen = {}

    class FakeCtx:
        def register_terminal_environment_provider(self, provider):
            seen["provider"] = provider

    lightbulb_node.register(FakeCtx())
    assert isinstance(seen["provider"], lightbulb_node.LightbulbNodeProvider)
