"""`lightbulb_node`: the terminal environment provider that runs a Hermes turn's shell
commands on the owner's paired desktop through the backend's node route.

Most of this module is plain and needs no Hermes image on the path (M2-R7: it never imports
`agent.terminal_env_provider` at module scope). Only `register()`/`_provider()` -- the "provider
factory" -- does that import, lazily, so those two tests stub `sys.modules["agent.terminal_env_provider"]`
with a minimal base class the way test_home_scope.py stubs `hermes_constants`; the real class was
read from the image (see the lane report for the exact `docker run` commands and names).
"""
import io
import json
import pathlib
import subprocess
import sys
import urllib.error
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


def test_stdin_mode_is_heredoc_for_tool_result_storages_plus_one_byte_tolerance(monkeypatch):
    assert env(monkeypatch, {"ok": True, "payload": {}})._stdin_mode == "heredoc"


def test_a_write_file_shaped_call_embeds_stdin_instead_of_silently_dropping_it(monkeypatch):
    """Fix round 1, item 1 (critical): the node host hardcodes an empty stdin buffer, so a
    caller like file_operations._atomic_write (`cat > "$tmp"`, stdin_data=content) got NOTHING
    when stdin_data was swallowed by execute()'s old **_kw -- silent data loss, not an error.
    The produced command must actually carry the content, not just the bare original command."""
    seen = {}
    monkeypatch.setattr(lightbulb_node, "_invoke",
                        lambda command, params, timeout_ms: seen.update(params=params) or
                        {"ok": True, "payload": {"stdout": "", "stderr": "", "exitCode": 0,
                                                 "timedOut": False, "success": True}})
    lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/x", timeout=180).execute(
        'cat > "$tmp"', stdin_data="the file content\n")
    cmd = seen["params"]["command"]
    assert cmd[:2] == ["bash", "-lc"]
    assert cmd[2] != 'cat > "$tmp"', "stdin_data was dropped -- the bare command carries no content"
    assert "the file content" in cmd[2]


def _run_heredoc_locally(command, params, timeout_ms):
    """Test double for _invoke: actually runs the constructed ['bash','-lc', cmd] through real
    bash, standing in for the paired Mac's node host, so heredoc-quoting bugs (expansion,
    delimiter collisions) show up as real behavior instead of string-shape assertions."""
    result = subprocess.run(params["command"], cwd=params.get("cwd") or None,
                            capture_output=True, text=True)
    return {"ok": True, "payload": {"stdout": result.stdout, "stderr": result.stderr,
                                    "exitCode": result.returncode, "timedOut": False}}


def test_heredoc_embedded_stdin_is_quoted_so_the_shell_never_expands_it(monkeypatch, tmp_path):
    out_file = tmp_path / "out.txt"
    monkeypatch.setattr(lightbulb_node, "_invoke", _run_heredoc_locally)
    env_obj = lightbulb_node.LightbulbNodeProvider().create_environment(cwd=str(tmp_path), timeout=30)
    out = env_obj.execute(f"cat > {out_file}", stdin_data="hello\n$HOME\n")
    assert out["returncode"] == 0
    # If the heredoc delimiter weren't quoted, $HOME would expand to the real home directory.
    # A heredoc's body always ends at its own line break before the delimiter, so content that
    # already ends in "\n" round-trips with one extra trailing byte -- the exact +1 that
    # tool_result_storage.py's own heredoc-mode tolerance exists for.
    assert out_file.read_text() == "hello\n$HOME\n\n"


def test_heredoc_delimiter_is_unique_so_body_text_resembling_it_still_round_trips(monkeypatch, tmp_path):
    out_file = tmp_path / "out.txt"
    decoy_delimiter_line = "HERMES_STDIN_deadbeefcafe"  # looks like our delimiter, isn't the real one
    content = f"line one\n{decoy_delimiter_line}\nline three\n"
    monkeypatch.setattr(lightbulb_node, "_invoke", _run_heredoc_locally)
    env_obj = lightbulb_node.LightbulbNodeProvider().create_environment(cwd=str(tmp_path), timeout=30)
    out = env_obj.execute(f"cat > {out_file}", stdin_data=content)
    assert out["returncode"] == 0
    assert out_file.read_text() == content + "\n"  # same +1 heredoc-trailer byte as above


def _fake_urlopen_raising(exc):
    def fake_urlopen(request, timeout=None):
        raise exc
    return fake_urlopen


def _set_node_env(monkeypatch):
    monkeypatch.setenv("ISOL8_API_URL", "https://api-dev.flickmeon.ai")
    monkeypatch.setenv("ISOL8_OWNER_ID", "org_1")
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "tok")


def test_an_http_error_from_the_node_route_surfaces_its_json_code_and_message(monkeypatch):
    """Fix round 1, item 2: the node route answers 404/409/403/504 with a JSON body
    ({"code": ..., "message": ...}) that urlopen raises as HTTPError, not a return value --
    _invoke's plain urlopen call let that body get discarded as a bare exception name."""
    _set_node_env(monkeypatch)
    body = json.dumps({"code": "NODE_NOT_ATTACHED", "message": "no paired desktop"}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_raising(
        urllib.error.HTTPError("http://x", 404, "Not Found", None, io.BytesIO(body))))
    env_obj = lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/x", timeout=30)
    out = env_obj.execute("ls")
    assert out["returncode"] == 1
    assert "NODE_NOT_ATTACHED" in out["output"] and "no paired desktop" in out["output"]


def test_a_non_json_http_error_body_falls_back_to_the_http_status(monkeypatch):
    _set_node_env(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_raising(
        urllib.error.HTTPError("http://x", 502, "Bad Gateway", None, io.BytesIO(b"<html>not json</html>"))))
    env_obj = lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/x", timeout=30)
    out = env_obj.execute("ls")
    assert out["returncode"] == 1
    assert "502" in out["output"]


def test_a_urlerror_still_yields_a_transport_failure_with_returncode_none(monkeypatch):
    _set_node_env(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_raising(
        urllib.error.URLError("connection refused")))
    env_obj = lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/x", timeout=30)
    out = env_obj.execute("ls")
    assert out["returncode"] is None
    assert "URLError" in out["output"]


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
    surface, stubbed the way test_home_scope.py stubs hermes_constants -- this test runner has
    no Hermes image on the path. Fix round 1, item 3: a real abc.ABC with `name` as an
    abstract @property and is_available/create_environment as abstract methods (exactly how
    the real base declares them), not a plain class -- so a future regression that drops one
    of LightbulbNodeProvider's overrides fails these tests with a TypeError at instantiation,
    the same way it would fail against the real Hermes image."""
    import abc
    import types

    fake_pkg = types.ModuleType("agent")
    fake_mod = types.ModuleType("agent.terminal_env_provider")

    class TerminalEnvironmentProvider(abc.ABC):
        is_remote = True
        is_container = True
        session_isolated_when_nonpersistent = False

        @property
        @abc.abstractmethod
        def name(self):
            ...

        @abc.abstractmethod
        def is_available(self):
            ...

        @abc.abstractmethod
        def create_environment(self, *, cwd, timeout, task_id="default", image=None,
                               container_config=None, **kwargs):
            ...

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
